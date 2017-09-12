import lasagne
import numpy as np
import theano
import theano.tensor as tt
from kusanagi import utils

randint = lasagne.random.get_rng().randint(1, 2147462579)
m_rng = theano.sandbox.rng_mrg.MRG_RandomStreams(randint)


def propagate_particles(x, pol, dyn, D, angle_dims=None, iid_per_eval=False):
    ''' Given a set of input states, this function returns predictions for
        the next states. This is done by 1) evaluating the current pol
        2) using the dynamics model to estimate the next state. If x has
        shape [n, D] where n is tthe number of samples and D is the state
        dimension, this function will return x_next with shape [n, D]
        representing the next states and costs with shape [n, 1]
    '''
    # convert angles from input states to their complex representation
    xa = utils.gTrig(x, angle_dims, D)

    # compute controls for each sample
    u = pol.evaluate(xa, symbolic=True,
                     iid_per_eval=iid_per_eval,
                     return_samples=True)

    # build state-control vectors
    xu = tt.concatenate([xa, u], axis=1)

    # predict the change in state given current state-control for each particle
    delta_x, sn_x = dyn.predict_symbolic(xu, iid_per_eval=iid_per_eval,
                                         return_samples=True)

    # compute the successor states
    x_next = x + delta_x

    return x_next, sn_x


def rollout(x0, H, gamma0,
            pol, dyn, cost,
            D, angle_dims=None, resample=True,
            z=None, truncate_gradient=-1,
            **kwargs):
    ''' Given some initial state particles x0, and a prediction horizon H
    (number of timesteps), returns a set of trajectories sampled from the
    dynamics model and the discounted costs for each step in the
    trajectory.
    '''
    msg = 'Building computation graph for state particles propagation'
    utils.print_with_stamp(msg, 'mc_pilco.rollout')

    # define internal scan computations
    def step_rollout(z, x, gamma, *args):
        '''
            Single step of rollout.
        '''
        # get next state distribution
        x_next, sn = propagate_particles(x, pol, dyn, D, angle_dims, **kwargs)

        #  get cost of applying action:
        n = x_next.shape[0]
        n = n.astype(theano.config.floatX)
        mx_next = x_next.mean(0)
        Sx_next = x_next.T.dot(x_next)/n - tt.outer(mx_next, mx_next)
        mc_next = cost(mx_next, Sx_next)[0]
        c_next = cost(x_next, None)
        #mc_next = c_next.mean(0)

        # resample if requested
        if resample:
            #z = m_rng.normal(x_next.shape)
            x_next = mx_next + z.dot(tt.slinalg.cholesky(Sx_next).T)

        return [gamma*mc_next, gamma*c_next, x_next, gamma*gamma0]

    # these are the shared variables that will be used in the scan graph.
    # we need to pass them as non_sequences here
    # see: http://deeplearning.net/software/theano/library/scan.html
    nseq = [gamma0]
    nseq.extend(dyn.get_intermediate_outputs())
    nseq.extend(pol.get_intermediate_outputs())

    # loop over the planning horizon
    output = theano.scan(fn=step_rollout,
                         sequences=[z],
                         outputs_info=[None, None, x0, gamma0],
                         non_sequences=nseq,
                         n_steps=H,
                         strict=True,
                         allow_gc=False,
                         truncate_gradient=truncate_gradient,
                         name="mc_pilco>rollout_scan")
    rollout_output, rollout_updts = output
    mcosts, costs, trajectories = rollout_output[:3]
    trajectories.name = 'trajectories'

    # first axis: batch, second axis: time step
    costs = costs.T
    # first axis; batch, second axis: time step
    trajectories = trajectories.transpose(1, 0, 2)

    return [mcosts, costs, trajectories], rollout_updts


def get_loss(pol, dyn, cost, D, angle_dims, n_samples=50,
             intermediate_outs=False, resample_particles=True, crn=True,
             resample_dyn=False, average=True, truncate_gradient=-1):
    '''
        Constructs the computation graph for the value function according to
        the mc-pilco algorithm:
        1) sample x0 from initial state distribution N(mx0,Sx0)
        2) propagate the state particles forward in time
            2a) compute controls for eachc particle
            2b) use dynamics model to predict next states for each particle
            2c) compute cost for each particle
        3) return the expected value of the sum of costs
        @param pol
        @param dyn
        @param cost
        @param D number of state dimensions, must be a python integer
        @param angle_dims angle dimensions that should be converted to complex
                          representation
        @return Returns a tuple of (outs, inps, updts). These correspond to the
                output variables, input variables and updates dictionary, if
                any.
                By default, the only output variable is the value.
    '''
    # make sure that the dynamics model has the same number of samples
    if hasattr(dyn, 'update'):
        dyn.update(n_samples)
    if hasattr(pol, 'update'):
        pol.update(n_samples)

    # initial state distribution
    mx0 = tt.vector('mx0')
    Sx0 = tt.matrix('Sx0')

    # prediction horizon
    H = tt.iscalar('H')
    # discount factor
    gamma = tt.scalar('gamma')

    # draw initial set of particles
    if crn:
        # use common random numbers
        # initialize z as a shared variable; i.e. z will be sampled once.
        z = theano.shared(
            np.random.normal(
                size=(100, n_samples, D)).astype(theano.config.floatX))

        # now we will make sure that z is has the correct shape
        z = theano.ifelse.ifelse(
            z.shape[0] < H,
            tt.tile(z, (tt.ceil(H/z.shape[0]).astype('int64'), 1, 1)),
            z
        )[:H]
    else:
        # new samples with every rollout
        z = m_rng.normal((H, n_samples, D))

    Lx0 = tt.slinalg.cholesky(Sx0)
    x0 = mx0 + z[0].dot(Lx0.T)

    # get rollout output
    r_outs, updts = rollout(x0, H, gamma,
                            pol, dyn, cost,
                            D, angle_dims,
                            resample=resample_particles,
                            z=z,
                            iid_per_eval=resample_dyn,
                            truncate_gradient=truncate_gradient)

    mean_costs, costs, trajectories = r_outs

    # loss is E_{dyns}((1/H)*sum c(x_t))
    #          = (1/H)*sum E_{x_t}(c(x_t))
    loss = mean_costs.mean() if average else mean_costs.sum()

    inps = [mx0, Sx0, H, gamma]
    if intermediate_outs:
        return [loss, costs, trajectories], inps, updts
    else:
        return loss, inps, updts


def build_rollout(*args, **kwargs):
    kwargs['intermediate_outs'] = True
    outs, inps, updts = get_loss(*args, **kwargs)
    rollout_fn = theano.function(inps, outs, updates=updts,
                                 allow_input_downcast=True)
    return rollout_fn
