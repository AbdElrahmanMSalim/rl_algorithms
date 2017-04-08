import numpy as np
import tensorflow as tf
import tensorflow.contrib.distributions as distr
import gym
import logz
import scipy.signal
import sys


def normc_initializer(std=1.0):
    """ Initialize array with normalized columns """
    def _initializer(shape, dtype=None, partition_info=None): #pylint: disable=W0613
        out = np.random.randn(*shape).astype(np.float32)
        out *= std / np.sqrt(np.square(out).sum(axis=0, keepdims=True))
        return tf.constant(out)
    return _initializer


def dense(x, size, name, weight_init=None):
    """ Dense (fully connected) layer """
    w = tf.get_variable(name + "/w", [x.get_shape()[1], size], initializer=weight_init)
    b = tf.get_variable(name + "/b", [size], initializer=tf.zeros_initializer())
    return tf.matmul(x, w) + b


def fancy_slice_2d(X, inds0, inds1):
    """ Like numpy's X[inds0, inds1] """
    inds0 = tf.cast(inds0, tf.int64)
    inds1 = tf.cast(inds1, tf.int64)
    shape = tf.cast(tf.shape(X), tf.int64)
    ncols = shape[1]
    Xflat = tf.reshape(X, [-1])
    return tf.gather(Xflat, inds0 * ncols + inds1)


def discount(x, gamma):
    """
    Compute discounted sum of future values. Returns a list, NOT a scalar!
    out[i] = in[i] + gamma * in[i+1] + gamma^2 * in[i+2] + ...
    """
    return scipy.signal.lfilter([1],[1,-gamma],x[::-1], axis=0)[::-1]


def explained_variance_1d(ypred,y):
    """
    Var[ypred - y] / var[y]. 
    https://www.quora.com/What-is-the-meaning-proportion-of-variance-explained-in-linear-regression
    """
    assert y.ndim == 1 and ypred.ndim == 1    
    vary = np.var(y)
    return np.nan if vary==0 else 1 - np.var(y-ypred)/vary


def categorical_sample_logits(logits):
    """
    Samples (symbolically) from categorical distribution, where logits is a NxK
    matrix specifying N categorical distributions with K categories

    specifically, exp(logits) / sum( exp(logits), axis=1 ) is the 
    probabilities of the different classes

    Cleverly uses gumbell trick, based on
    https://github.com/tensorflow/tensorflow/issues/456
    """
    U = tf.random_uniform(tf.shape(logits))
    return tf.argmax(logits - tf.log(-tf.log(U)), dimension=1)


def pathlength(path):
    return len(path["reward"])


class LinearValueFunction(object):
    """ Estimates the baseline function for PGs via ridge regression. """
    coef = None

    def fit(self, X, y):
        """ Updates weights (self.coef) with design matrix X (i.e. observations)
        and targets (i.e. actual returns) y. """
        Xp = self.preproc(X)
        A = Xp.T.dot(Xp)
        nfeats = Xp.shape[1]
        A[np.arange(nfeats), np.arange(nfeats)] += 1e-3 # a little ridge regression
        b = Xp.T.dot(y)
        self.coef = np.linalg.solve(A, b)

    def predict(self, X):
        """ Predicts return from observations (i.e. environment states) X. """
        if self.coef is None:
            return np.zeros(X.shape[0])
        else:
            return self.preproc(X).dot(self.coef)

    def preproc(self, X):
        """ Adding a bias column, and also adding squared values (huh). """
        return np.concatenate([np.ones([X.shape[0], 1]), X, np.square(X)/2.0], axis=1)


class NnValueFunction(object):
    """ Estimates the baseline function for PGs via neural network. """
    pass # YOUR CODE HERE


def lrelu(x, leak=0.2):
    """ Performs a leaky ReLU operation. """
    f1 = 0.5 * (1 + leak)
    f2 = 0.5 * (1 - leak)
    return f1 * x + f2 * abs(x)


def main_cartpole(n_iter=100, gamma=1.0, min_timesteps_per_batch=1000, 
                  stepsize=1e-2, animate=True, logdir=None):
    """ Runs vanilla policy gradient on the classic CartPole task.

    Symbolic variables have the prefix sy_, to distinguish them from the
    numerical values that are computed later in this function. Symbolic means
    that TF will not "compute values" until run in a session. Naming convention
    for shapes: `n` means batch size, `o` means observation dim, `a` means
    action dim. Also, some of these (e.g. sy_ob_no) are used both for when
    running the policy AND during training with a batch of observations.
    
      sy_ob_no:        batch of observations
      sy_ac_n:         batch of actions taken by the policy, for policy gradient computation
      sy_adv_n:        advantage function estimate
      sy_h1:           hidden layer (before this: input -> dense -> relu)
      sy_logits_na:    logits describing probability distribution of final layer
      sy_oldlogits_na: logits before updating, only for KL diagnostic
      sy_logp_na:      log probability of actions
      sy_sampled_ac:   sampled action when running the policy (NOT computing the policy gradient)
      sy_n:            clever way to obtain the batch size
      sy_logprob_n:    log-prob of actions taken -- used for policy gradient calculation

    Some of these rely on our convenience methods. Use a small initialization
    for the last layer, so the initial policy has maximal entropy. We are
    defaulting to a fully connected policy network with one hidden layer of 32
    units, and a softmax output (by default, applied to the last dimension,
    which we want here). Then we define a surrogate loss function. Again, it's
    the same as before, define a loss function and plug it into Adam.
   
    Args:
        n_iter: Number of iterations for policy gradient.
        gamma: The discount factor, used for computing returns.
        min_timesteps_per_batch: Minimum number of timesteps in a given
            iteration of policy gradients. Each trajectory consists of multiple
            timesteps.
        stepsize:
        animate: Whether to render it in OpenAI gym.
        logdir: Output directory for logging. If None, store to a random place.
    """
    env = gym.make("CartPole-v0")
    ob_dim = env.observation_space.shape[0]
    num_actions = env.action_space.n
    logz.configure_output_dir(logdir)
    vf = LinearValueFunction()

    # Symbolic variables as covered in the method documentation:
    sy_ob_no        = tf.placeholder(shape=[None, ob_dim], name="ob", dtype=tf.float32)
    sy_ac_n         = tf.placeholder(shape=[None], name="ac", dtype=tf.int32) 
    sy_adv_n        = tf.placeholder(shape=[None], name="adv", dtype=tf.float32)
    sy_h1           = lrelu(dense(sy_ob_no, 32, "h1", weight_init=normc_initializer(1.0)))
    sy_logits_na    = dense(sy_h1, num_actions, "final", weight_init=normc_initializer(0.05))
    sy_oldlogits_na = tf.placeholder(shape=[None, num_actions], name='oldlogits', dtype=tf.float32)
    sy_logp_na      = tf.nn.log_softmax(sy_logits_na)
    sy_sampled_ac   = categorical_sample_logits(sy_logits_na)[0]
    sy_n            = tf.shape(sy_ob_no)[0]
    sy_logprob_n    = fancy_slice_2d(sy_logp_na, tf.range(sy_n), sy_ac_n)

    # The following quantities are just used for computing KL and entropy, JUST FOR DIAGNOSTIC PURPOSES >>>>
    sy_oldlogp_na = tf.nn.log_softmax(sy_oldlogits_na)
    sy_oldp_na    = tf.exp(sy_oldlogp_na) 
    sy_kl         = tf.reduce_sum(sy_oldp_na * (sy_oldlogp_na - sy_logp_na)) / tf.to_float(sy_n)
    sy_p_na       = tf.exp(sy_logp_na)
    sy_ent        = tf.reduce_sum( - sy_p_na * sy_logp_na) / tf.to_float(sy_n)
    # <<<<<<<<<<<<<

    # Loss function that we'll differentiate to get the policy gradient ("surr" is for "surrogate loss")
    sy_surr = - tf.reduce_mean(sy_adv_n * sy_logprob_n) 

    # Symbolic, in case you want to change the stepsize during optimization. (We're not doing that currently)
    sy_stepsize = tf.placeholder(shape=[], dtype=tf.float32) 
    update_op = tf.train.AdamOptimizer(sy_stepsize).minimize(sy_surr)

    # use single thread. on such a small problem, multithreading gives you a slowdown
    # this way, we can better use multiple cores for different experiments
    tf_config = tf.ConfigProto(inter_op_parallelism_threads=1, intra_op_parallelism_threads=1) 
    sess = tf.Session(config=tf_config)
    sess.__enter__() # equivalent to `with sess:`
    tf.global_variables_initializer().run() #pylint: disable=E1101

    total_timesteps = 0

    for i in range(n_iter):
        print("********** Iteration %i ************"%i)

        # Collect paths until we have enough timesteps.
        timesteps_this_batch = 0
        paths = []
        while True:
            ob = env.reset()
            terminated = False
            obs, acs, rewards = [], [], []
            animate_this_episode=(len(paths)==0 and (i % 10 == 0) and animate)
            while True:
                if animate_this_episode:
                    env.render()
                obs.append(ob)
                ac = sess.run(sy_sampled_ac, feed_dict={sy_ob_no : ob[None]})
                acs.append(ac)
                ob, rew, done, _ = env.step(ac)
                rewards.append(rew)
                if done:
                    break                    
            path = {"observation" : np.array(obs), "terminated" : terminated,
                    "reward" : np.array(rewards), "action" : np.array(acs)}
            paths.append(path)
            timesteps_this_batch += pathlength(path)
            if timesteps_this_batch > min_timesteps_per_batch:
                break
        total_timesteps += timesteps_this_batch

        # Estimate advantage function using baseline vf (these are lists!).
        vtargs, vpreds, advs = [], [], []
        for path in paths:
            rew_t = path["reward"]
            return_t = discount(rew_t, gamma)
            vpred_t = vf.predict(path["observation"])
            adv_t = return_t - vpred_t
            advs.append(adv_t)
            vtargs.append(return_t)
            vpreds.append(vpred_t)

        # Build arrays for policy update and also re-fit the baseline.
        ob_no = np.concatenate([path["observation"] for path in paths])
        ac_n = np.concatenate([path["action"] for path in paths])
        adv_n = np.concatenate(advs)
        standardized_adv_n = (adv_n - adv_n.mean()) / (adv_n.std() + 1e-8)
        vtarg_n = np.concatenate(vtargs)
        vpred_n = np.concatenate(vpreds)
        vf.fit(ob_no, vtarg_n)

        # Policy update
        _, oldlogits_na = sess.run([update_op, sy_logits_na], 
                                   feed_dict={sy_ob_no:ob_no, 
                                              sy_ac_n:ac_n, 
                                              sy_adv_n:standardized_adv_n, 
                                              sy_stepsize:stepsize
                                   })
        kl, ent = sess.run([sy_kl, sy_ent], 
                           feed_dict={sy_ob_no:ob_no, 
                                      sy_oldlogits_na:oldlogits_na
                           })

        # Log diagnostics
        logz.log_tabular("EpRewMean", np.mean([path["reward"].sum() for path in paths]))
        logz.log_tabular("EpLenMean", np.mean([pathlength(path) for path in paths]))
        logz.log_tabular("KLOldNew", kl)
        logz.log_tabular("Entropy", ent)
        logz.log_tabular("EVBefore", explained_variance_1d(vpred_n, vtarg_n))
        logz.log_tabular("EVAfter", explained_variance_1d(vf.predict(ob_no), vtarg_n))
        logz.log_tabular("TimestepsSoFar", total_timesteps)
        # If you're overfitting, EVAfter will be way larger than EVBefore.
        # Note that we fit value function AFTER using it to compute the advantage function to avoid introducing bias
        logz.dump_tabular()


def main_pendulum(logdir, seed, n_iter, gamma, min_timesteps_per_batch, 
                  initial_stepsize, desired_kl, vf_type, vf_params, animate=False):
    """ Runs policy gradients using a continuous action space now.

    Here are (some of) the symbolic variables I will be using. Convention for
    shaping is to use `n` (batch size), `o`, and/or `a` in the symbolic names.
    The following are for both the gradient and the policy:

      sy_ob_no:       batch of observations, both for PG computation AND running policy
      sy_h1:          hidden layer (before this: input -> dense -> relu)
      sy_mean_na:     final net output, the mean of a Gaussian, NOT a probability distribution

    The following are for the policy, but not the gradient:

      sy_sampled_ac:  the current sampled action (a vector of controls) when running policy

    The following are for the gradient, but not the policy:

      sy_ac_na:       batch of actions taken by the policy, for PG computation
      sy_adv_n:       advantage function estimate (one per action vector)
      sy_n:           clever way to obtain the batch size during training
      sy_logprob_n:   log-prob of actions taken in the batch, for PG computation

    Here's the idea. Our parameters consists of (neural network weights, log std
    vector). The policy network will output the _mean_ of a Gaussian, NOT our
    actual action. Then the next set of parameters is the log std. Those two
    (the mean and log std) together define a distribution which we then sample
    from to get the actual action vector the agent plays. Tricky: realize that
    sy_mean_na (output of the net) is a symbolic variable and thus NOT a
    parameter, but logstd_a IS a parameter. The log is useful so we can directly
    use it when computing log probs.

    For managing the distribution, I'm using tf.contrib.distributions. We can
    call the sample() method which will give us a tensor (I think a symbolic
    variable). We _can_ put in a batch of means/stdevs into the distribution,
    and when we sample from it, we'll get one sample per item in the batch.
    However, I'm confused about if we have to provide the same standard
    deviation value for each? That seems the only way to do things.
    """
    tf.set_random_seed(seed)
    np.random.seed(seed)
    env = gym.make("Pendulum-v0")
    ob_dim = env.observation_space.shape[0]
    ac_dim = env.action_space.shape[0]
    logz.configure_output_dir(logdir)
    if vf_type == 'linear':
        vf = LinearValueFunction(**vf_params)
    elif vf_type == 'nn':
        vf = NnValueFunction(ob_dim=ob_dim, **vf_params)

    # This is still part of the parameters! It's not symbolic, of course. Also,
    # the homework in the class website is outdated; missing `()` at the end.
    logstd_a = tf.get_variable("logstdev", [ac_dim], initializer=tf.zeros_initializer())

    # Set up some symbolic variables (i.e placeholders).
    sy_ob_no      = tf.placeholder(shape=[None, ob_dim], name="ob", dtype=tf.float32)
    sy_ac_na      = tf.placeholder(shape=[None, ac_dim], name="ac", dtype=tf.int32) 
    sy_adv_n      = tf.placeholder(shape=[None], name="adv", dtype=tf.float32)
    sy_h1         = lrelu(dense(sy_ob_no, 32, "h1", weight_init=normc_initializer(1.0)))
    sy_mean_na    = dense(sy_h1, ac_dim, "mean", weight_init=normc_initializer(0.05))
    sy_n          = tf.shape(sy_ob_no)[0]

    # Set up the Gaussian distribution. It's not a symbolic variable, I think.
    # Also, sy_mean_na.shape = (bs,ac_dim) so I think the standard deviation
    # also has to have bs as the first dimension. Another idea: maybe explicitly
    # compute the log prob, rather than calling gauss_policy.pdf? It might more
    # numerically stable. ALSO how would we handle this with a single input?
    # Think carefully about this section ...
    std_batch     = tf.ones((sy_n,ac_dim)) * tf.exp(logstd_a)
    gauss_policy  = distr.MultivariateNormalDiag(mu=sy_mean_na, diag_stdev=std_batch)
    sy_sampled_ac = gauss_policy.sample()
    sy_logprob_n  = tf.log(gauss_policy.pdf(sy_ac_na) + 0.00000001)

    # The following quantities are used for computing KL and entropy. Note that
    # unlike the cartpole setting, here we're actually using these. The sy_ent
    # is differential entropy.
    # sy_oldlogp_na = tf.nn.log_softmax(sy_oldlogits_na)
    # sy_oldp_na    = tf.exp(sy_oldlogp_na) 
    # sy_kl         = tf.reduce_sum(sy_oldp_na * (sy_oldlogp_na - sy_logp_na)) / tf.to_float(sy_n)
    # sy_p_na       = tf.exp(sy_logp_na)
    # sy_ent        = tf.reduce_sum( - sy_p_na * sy_logp_na) / tf.to_float(sy_n)

    # Loss function that we'll differentiate to get the policy gradient ("surr" is for "surrogate loss")
    sy_surr = - tf.reduce_mean(sy_adv_n * sy_logprob_n) 

    # Symbolic, in case you want to change the stepsize during optimization. (We're not doing that currently)
    sy_stepsize = tf.placeholder(shape=[], dtype=tf.float32) 
    update_op = tf.train.AdamOptimizer(sy_stepsize).minimize(sy_surr)

    sess = tf.Session()
    sess.__enter__() # equivalent to `with sess:`
    tf.global_variables_initializer().run() #pylint: disable=E1101
    total_timesteps = 0
    stepsize = initial_stepsize

    # Debugging.
    print("sy_ac_na.shape = {}".format(sy_ac_na.get_shape()))
    print("sy_mean_na.shape = {}".format(sy_mean_na.get_shape()))
    print("std_batch.shape = {}".format(std_batch.get_shape()))
    print("tf.exp(logstd_a).shape = {}".format(tf.exp(logstd_a).get_shape()))
    print("sy_sampled_ac.shape = {}".format(sy_sampled_ac.get_shape()))
    print("sy_logprob_n.shape = {}".format(sy_logprob_n.get_shape()))
    sys.exit()

    for i in range(n_iter):
        print("********** Iteration %i ************"%i)

        ### YOUR_CODE_HERE

        if kl > desired_kl * 2: 
            stepsize /= 1.5
            print('stepsize -> %s'%stepsize)
        elif kl < desired_kl / 2: 
            stepsize *= 1.5
            print('stepsize -> %s'%stepsize)
        else:
            print('stepsize OK')

        # Log diagnostics
        logz.log_tabular("EpRewMean", np.mean([path["reward"].sum() for path in paths]))
        logz.log_tabular("EpLenMean", np.mean([pathlength(path) for path in paths]))
        logz.log_tabular("KLOldNew", kl)
        logz.log_tabular("Entropy", ent)
        logz.log_tabular("EVBefore", explained_variance_1d(vpred_n, vtarg_n))
        logz.log_tabular("EVAfter", explained_variance_1d(vf.predict(ob_no), vtarg_n))
        logz.log_tabular("TimestepsSoFar", total_timesteps)
        # If you're overfitting, EVAfter will be way larger than EVBefore.
        # Note that we fit value function AFTER using it to compute the advantage function to avoid introducing bias
        logz.dump_tabular()


def main_pendulum1(d):
    return main_pendulum(**d)


if __name__ == "__main__":
    """ Have to get this better-organized ... """
    if 0:
        main_cartpole(logdir=None) # when you want to start collecting results, set the logdir
    if 1:
        general_params = dict(gamma=0.97, animate=False, min_timesteps_per_batch=2500, n_iter=300, initial_stepsize=1e-3)
        more_params = dict(logdir=None, seed=0, desired_kl=2e-3, vf_type='linear', vf_params={}, **general_params)
        main_pendulum(**more_params) # when you want to start collecting results, set the logdir
    if 0:
        general_params = dict(gamma=0.97, animate=False, min_timesteps_per_batch=2500, n_iter=300, initial_stepsize=1e-3)
        params = [
            dict(logdir='/tmp/ref/linearvf-kl2e-3-seed0', seed=0, desired_kl=2e-3, vf_type='linear', vf_params={}, **general_params),
            dict(logdir='/tmp/ref/nnvf-kl2e-3-seed0', seed=0, desired_kl=2e-3, vf_type='nn', vf_params=dict(n_epochs=10, stepsize=1e-3), **general_params),
            dict(logdir='/tmp/ref/linearvf-kl2e-3-seed1', seed=1, desired_kl=2e-3, vf_type='linear', vf_params={}, **general_params),
            dict(logdir='/tmp/ref/nnvf-kl2e-3-seed1', seed=1, desired_kl=2e-3, vf_type='nn', vf_params=dict(n_epochs=10, stepsize=1e-3), **general_params),
            dict(logdir='/tmp/ref/linearvf-kl2e-3-seed2', seed=2, desired_kl=2e-3, vf_type='linear', vf_params={}, **general_params),
            dict(logdir='/tmp/ref/nnvf-kl2e-3-seed2', seed=2, desired_kl=2e-3, vf_type='nn', vf_params=dict(n_epochs=10, stepsize=1e-3), **general_params),
        ]
        import multiprocessing
        p = multiprocessing.Pool()
        p.map(main_pendulum1, params)
