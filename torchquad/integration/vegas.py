from autoray import numpy as anp
from autoray import infer_backend
from loguru import logger


from .base_integrator import BaseIntegrator
from .utils import _setup_integration_domain, RNG
from .vegas_map import VEGASMap
from .vegas_stratification import VEGASStratification


class VEGAS(BaseIntegrator):
    """VEGAS Enhanced. Refer to https://arxiv.org/abs/2009.05112 .
    Implementation inspired by https://github.com/ycwu1030/CIGAR/ .
    EQ <n> refers to equation <n> in the above paper.
    """

    def __init__(self):
        super().__init__()

    def integrate(
        self,
        fn,
        dim,
        N=10000,
        integration_domain=None,
        seed=None,
        use_grid_improve=True,
        eps_rel=0,
        eps_abs=0,
        max_iterations=20,
        use_warmup=True,
        backend="torch",
    ):
        """Integrates the passed function on the passed domain using VEGAS.

        Args:
            fn (func): The function to integrate over.
            dim (int): Dimensionality of the function to integrate.
            N (int, optional): Maximum number of function evals to use for the integration. Defaults to 10000.
            integration_domain (list, optional): Integration domain, e.g. [[-1,1],[0,1]]. Defaults to [-1,1]^dim.
            seed (int, optional): Random number generation seed to the sampling point creation, only set if provided. Defaults to None.
            use_grid_improve (bool, optional): If True will improve the grid after each iteration.
            eps_rel (float, optional): Relative error to abort at. Defaults to 0.
            eps_abs (float, optional): Absolute error to abort at. Defaults to 0.
            max_iterations (int, optional): Maximum number of vegas iterations to perform. Defaults to 32.
            use_warmup (bool, optional): If a warmup should be used to initialize the map. Defaults to True.
            backend (string, optional): Numerical backend. This argument is ignored if the backend can be inferred from integration_domain. Defaults to "torch".

        Raises:
            ValueError: If len(integration_domain) != dim

        Returns:
            torch.Tensor: Integral value
        """

        self._check_inputs(dim=dim, N=N, integration_domain=integration_domain)
        logger.debug(
            "\n VEGAS integrating a "
            + str(dim)
            + "-dimensional fn with "
            + str(N)
            + " points over "
            + str(integration_domain)
            + "\n",
        )

        self._dim = dim
        self._nr_of_fevals = 0
        self._max_iterations = max_iterations
        self.use_grid_improve = use_grid_improve
        self.N = N
        # try to do as many evals in as many iterations as requested
        self._starting_N = N // self._max_iterations
        self._N_increment = N // self._max_iterations
        self._fn = fn
        self._integration_domain = integration_domain = _setup_integration_domain(
            dim, integration_domain, backend
        )
        self.backend = infer_backend(integration_domain)
        self.dtype = integration_domain.dtype
        self.rng = RNG(backend=self.backend, seed=seed)

        # Initialize the adaptive VEGAS map,
        # Note that a larger number of intervals may lead to problems if only few evals are allowed
        # Paper section II B
        N_intervals = max(2, self._N_increment // 10)  # for small N intervals set 2
        self.map = VEGASMap(
            self._dim, self._integration_domain, N_intervals=N_intervals
        )

        # Initialize VEGAS' stratification
        # Paper section III
        self.strat = VEGASStratification(
            self._N_increment,
            dim=self._dim,
            rng=self.rng,
            backend=self.backend,
            dtype=self.dtype,
        )

        logger.debug("Starting VEGAS")

        self.results = []  # contains integration results per iteration
        self.sigma2 = []  # contains variance per iteration

        self.it = 0  # iteration

        if use_warmup:  # warmup the adaptive map
            self._warmup_grid(5, self._starting_N // 5)

        # Main loop
        while True:
            self.it = self.it + 1
            self.results.append(0)
            self.sigma2.append(0)

            # Compute current iteration
            acc = self._run_iteration()

            logger.info(
                f"Iteration {self.it}, Acc={acc:.4e}, Result={self.results[-1]:.4e},neval={self._nr_of_fevals}"
            )

            # Abort conditions
            if self.it > self._max_iterations:
                break
            if self._nr_of_fevals > self.N - self._starting_N:
                break

            # Additional abort conditions depending on achieved errors
            if self.it % 5 == 0:
                res = self._get_result()
                err = self._get_error()
                chi2 = self._get_chisq()
                acc = err / res

                if anp.isnan(acc):  # capture 0 error
                    acc = anp.array(0.0, dtype=acc.dtype, like=acc)

                # Abort if errors acceptable
                logger.debug(f"Iteration {self.it},Chi2={chi2:.4e}")
                if (acc < eps_rel or err < eps_abs) and chi2 / 5.0 < 1.0:
                    break

                # Adjust number of evals if Chi square indicates instability
                # EQ 32
                if chi2 / 5.0 < 1.0:
                    self._starting_N = anp.minimum(
                        anp.array(
                            self._starting_N + self._N_increment,
                            dtype=acc.dtype,
                            like=acc,
                        ),
                        self._starting_N * anp.sqrt(acc / (eps_rel + 1e-8)),
                    )
                    self.results = []  # reset sample results
                    self.sigma2 = []  # reset sample results
                    continue
                elif chi2 / 5.0 > 1.0:
                    self._starting_N += self._N_increment
                    self.results = []  # reset sample results
                    self.sigma2 = []  # reset sample results
                    continue

        logger.info(
            f"Computed integral after {self._nr_of_fevals} evals was {self._get_result():.8e}."
        )
        return self._get_result()

    def _warmup_grid(self, warmup_N_it=5, N_samples=1000):
        """This function warms up the adaptive map of VEGAS over some iterations and samples.

        Args:
            warmup_N_it (int, optional): Number of warmup iterations. Defaults to 5.
            N_samples (int, optional): Number of samples per warmup iteration. Defaults to 1000.
        """
        logger.debug(
            f"Running Map Warmup with warmup_N_it={warmup_N_it}, N_samples={N_samples}..."
        )

        alpha_start = 0.5  # initial alpha value
        # TODO in the original paper this is adjusted over time
        self.alpha = alpha_start

        # Warmup
        logger.debug(
            "|  Iter  |    N_Eval    |     Result     |      Error     |    Acc        | Total Evals"
        )
        for warmup_iter in range(warmup_N_it):
            self.results.append(0)
            self.sigma2.append(0)
            jf = 0  # jacobians * function
            jf2 = 0

            # Sample points yrnd and transformed sample points x
            # Multiplying by 0.99999999 as the edge case of y=1 leads to an error
            yrnd = (
                self.rng.uniform(size=[N_samples, self._dim], dtype=self.dtype)
                * 0.999999
            )
            x = self.map.get_X(yrnd)
            f_eval = self._eval(x).squeeze()
            jac = self.map.get_Jac(yrnd)
            jf_vec = f_eval * jac
            jf_vec2 = jf_vec ** 2
            self.map.accumulate_weight(yrnd, jf_vec2)  # update map weights
            jf = jf_vec.sum()
            jf2 = jf_vec2.sum()

            ih = jf / N_samples  # integral in this step
            sig2 = jf2 / N_samples - pow(jf / N_samples, 2)  # estimated variance
            self.results[-1] += ih  # store results
            self.sigma2[-1] += sig2 / N_samples  # store results
            self.map.update_map()  # adapt the map
            # TODO fix for integrals close to 0
            acc = anp.sqrt(
                self.sigma2[-1] / self.results[-1]
            )  # compute estimated accuracy,
            logger.debug(
                f"|\t{warmup_iter}|         {N_samples}|  {self.results[-1]:5e}  |  {self.sigma2[-1]:5e}  |  {acc:4e}%| {self._nr_of_fevals}"
            )

        self.results.clear()
        self.sigma2.clear()

    def _run_iteration(self):
        """Runs one iteration of VEGAS including stratification and updates the VEGAS map if use_grid_improve is set.

        Returns:
            backend tensor float: Estimated accuracy.
        """
        neval = self.strat.get_NH(self._starting_N)  # Evals per strat cube
        self.starting_N = anp.sum(neval) / self.strat.N_cubes  # update real neval
        self._nr_of_fevals += neval.sum()  # Locally track function evals

        # Stratified sampling points y and transformed sample points x
        y = self.strat.get_Y(neval)
        x = self.map.get_X(y)  # transform, EQ 8+9
        f_eval = self._eval(x).squeeze()  # eval integrand

        jac = self.map.get_Jac(y)  # compute jacobian
        jf_vec = f_eval * jac  # precompute product once
        jf_vec2 = jf_vec ** 2

        if self.use_grid_improve:  # if adaptive map is used, acc weight
            self.map.accumulate_weight(y, jf_vec2)  # EQ 25
        jf, jf2 = self.strat.accumulate_weight(neval, jf_vec)  # update strat

        ih = (jf / neval) * self.strat.V_cubes  # Compute integral per cube

        # Collect results
        sig2 = (jf2 / neval) * (self.strat.V_cubes ** 2) - pow(ih, 2)
        self.results[-1] = ih.sum()  # store results
        self.sigma2[-1] = (sig2 / neval).sum()

        if self.use_grid_improve:  # if on, update adaptive map
            logger.debug("Running grid improvement")
            self.map.update_map()

        self.strat.update_DH()  # update stratification
        acc = anp.sqrt(self.sigma2[-1] / (self.results[-1]))  # estimate accuracy

        return acc

    # Helper funcs
    def _get_result(self):
        """Computes mean of results to estimate integral, EQ 30.

        Returns:
            backend tensor float: Estimated integral.
        """
        res_num = 0
        res_den = 0
        for idx, res in enumerate(self.results):
            res_num += res / self.sigma2[idx]
            res_den += 1.0 / self.sigma2[idx]

        if anp.isnan(res_num / res_den):  # if variance is 0 just return mean result
            return anp.mean(anp.array(self.results, dtype=res_num.dtype, like=res_num))
        else:
            return res_num / res_den

    def _get_error(self):
        """Estimates error from variance , EQ 31.

        Returns:
            backend tensor float: Estimated error.

        """
        res = 0
        for sig in self.sigma2:
            res += 1.0 / sig

        return 1.0 / anp.sqrt(res)

    def _get_chisq(self):
        """Computes chi square from estimated integral and variance, EQ 32.

        Returns:
            backend tensor float: Chi squared.
        """
        I_final = self._get_result()
        chi2 = 0
        for idx, res in enumerate(self.results):
            chi2 += pow(res - I_final, 2) / self.sigma2[idx]
        return chi2
