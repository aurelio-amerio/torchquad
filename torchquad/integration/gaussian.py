import torch
import numpy
import scipy
from scipy import special
from loguru import logger
from autoray import numpy as anp
from autoray import do
from .base_integrator import BaseIntegrator
from .utils import _setup_integration_domain

class Gaussian(BaseIntegrator):
    """Gaussian quadrature methods inherit from this. Default behaviour is Gauss-Legendre quadrature on [-1,1]."""

    def __init__(self):
        super().__init__()
        self.name = "Gauss-Legendre"
        self.root_fn = numpy.polynomial.legendre.leggauss
        self.root_args = ()
        self.default_integration_domain = [[-1, 1]]
        self.transform_interval = True
        self._cache = {}
    
    # credit for the idea https://github.com/scipy/scipy/blob/dde50595862a4f9cede24b5d1c86935c30f1f88a/scipy/integrate/_quadrature.py#L72
    def _cached_roots(self, root_args):
        """
        Cache polynomial results to speed up integration.
        """
        if root_args in self._cache:
            return self._cache[root_args]

        self._cache[root_args] = self.root_fn(*root_args)
        return self._cache[root_args]

    def _points_and_weights(self, root_args, wrapper_func=None):
        """Returns points and weights for integration.

        Args:
            root_args (tuple): arguments required for root-finding function, most commonly the degree N
            wrapper_func (func,optional): function that performs any additional calculations required to get the proper points and weights, eg. for use in Gauss-Lobatto quadrature, or for transformation of interval in Gauss-Legendre quadrature. Default is None.

        Returns:
            tuple(points, weights)
            """
        xi, wi = self._cached_roots(root_args)  # can autoray work with scipy functions?
        if isinstance(self._integration_domain, torch.Tensor):
            device = self._integration_domain.device
            xi = torch.from_numpy(xi).to(device)
            wi = torch.from_numpy(wi).to(device)
        if wrapper_func is not None:
            xi, wi = wrapper_func(xi, wi)
        if xi.shape[0] != self._dim:
            xi = self._expand_dimension(xi)
        if wi.shape[0] != self._dim:
            wi = self._expand_dimension(wi)

        return xi, wi

    def _expand_dimension(self, x):
        '''expand dimensions of 1D array or tensor'''
        try:
            x = do(
                "repeat",
                x,
                self._dim,
                like="numpy").reshape(
                self._dim,
                x.shape[0])
        except TypeError:  # need tensor to be on cpu, then assign it back to GPU
            x = do(
                "repeat",
                x.cpu().detach(),
                self._dim,
                like="numpy").reshape(
                self._dim,
                x.shape[0]).to(
                x.device)
        return x

    def integrate(self, fn, dim, args=None, N=8, integration_domain=None, backend=None):
        """Integrates the passed function on the passed domain using fixed-point Gaussian quadrature.

        Args:
            fn (func): The function to integrate over.
            dim (int): Dimensionality of the function to integrate.
            args (iterable object, optional): Additional arguments ``t0, ..., tn``, required by `fn`.
            N (int, optional): Degree to use for computing sample points and weights. Defaults to 8.
            integration_domain (list, optional): Integration domain, e.g. [[-1,1],[0,1]]. Defaults to [-1,1]^dim.

        Returns:
            float: integral value

        """
        if integration_domain is None or self.name in [
                "Gauss-Laguerre", "Gauss-Hermite"]:
            integration_domain = self.default_integration_domain * dim
            if self.name in ["Gauss-Laguerre", "Gauss-Hermite"]:
                logger.info(
                    f"{self.name} integration only allowed over the interval {self.default_integration_domain[0]}!")

        self._integration_domain = _setup_integration_domain(
            dim, integration_domain, backend)
        self._check_inputs(
            dim=dim,
            N=N,
            integration_domain=self._integration_domain)

        logger.debug(
            f"Using {self.name} for integrating a fn with {N} points over {self._integration_domain}")

        self._dim = dim
        self._fn = fn
        const = self._get_constant_multiplier()

        root_args = (N,) + self.root_args

        xi, wi = self._points_and_weights(
            root_args, wrapper_func=self.wrapper_func)

        # what if there is a sum in the function? then wi*self._eval() will
        # have dimension mismatch
        integral = anp.sum(
            const *
            anp.sum(
                self._eval(
                    xi,
                    args=args,
                    weights=wi),
                axis=1))
        logger.info(f"Computed integral was {integral}.")

        return integral


class GaussLegendre(Gaussian):
    """Gauss Legendre quadrature rule in torch. See https://en.wikipedia.org/wiki/Gaussian_quadrature#Gauss%E2%80%93Legendre_quadrature.

    Examples
    --------
    >>> gl=torchquad.GaussLegendre()
    >>> integral = gl.integrate(lambda x:np.sin(x), dim=1, N=101, integration_domain=[[0,5]]) #integral from 0 to 5 of np.sin(x)
    |TQ-INFO| Computed integral was 0.7163378000259399 #analytic result = 1-np.cos(5)"""

    def __init__(self):
        super().__init__()

    def wrapper_func(self, xi, wi):  # scale from [-1,1] to [a,b]
        a, b = self._integration_domain.T
        x0 = 0.5 * (1.0 - xi)  # self.points)
        x1 = 0.5 * (1.0 + xi)  # self.points)
        xi = anp.outer(a, x0) + anp.outer(b, x1)
        return xi, wi

    def _get_constant_multiplier(self):
        """Following a change of interval from [-1,1] to [a,b], the sum :math:`\\sum_{i=1}^{n} \omega_i f(x'_{i})` must be multiplied by the constant factor :math:`\frac{b-a}{2}`"""
        diff = self._integration_domain.T[1] - self._integration_domain.T[0]
        return 0.5 * diff


class GaussJacobi(Gaussian):
    """Gauss-Jacobi quadrature rule in torch, for integrals of the form :math:`\\int_{a}^{b} f(x) (1-x)^{\alpha} (1+x)^{\beta} dx`. See https://en.wikipedia.org/wiki/Gauss%E2%80%93Jacobi_quadrature.

    Examples
    --------
    >>> gj=torchquad.GaussJacobi(2,3)
    >>> integral = gj.integrate(lambda x:x, dim=1, N=101, integration_domain=[[0,5]]) #integral from 0 to 5 of x * (1-x)**2 * (1+x)**3
    |TQ-INFO| Computed integral was 7.61904761904762 #analytic result = 1346/105 #wrong?
    """

    def __init__(self, alpha, beta):
        super().__init__()
        self.name = "Gauss-Jacobi"
        self.root_fn = scipy.special.roots_jacobi
        self.root_args = (alpha, beta)

    def wrapper_func(self, xi, wi):  # scale from [-1,1] to [a,b]
        a, b = self._integration_domain.T
        x0 = 0.5 * (1.0 - xi)  # self.points)
        x1 = 0.5 * (1.0 + xi)  # self.points)
        xi = anp.outer(a, x0) + anp.outer(b, x1)
        return xi, wi

    def _get_constant_multiplier(self):
        """Following a change of interval from [-1,1] to [a,b], the sum :math:`\\sum_{i=1}^{n} \omega_i f(x'_{i})` must be multiplied by the constant factor :math:`\frac{b-a}{2}`"""
        diff = self._integration_domain.T[1] - self._integration_domain.T[0]
        return 0.5 * diff


class GaussLaguerre(Gaussian):
    """Gauss Laguerre quadrature rule in torch, for integrals of the form :math:`\\int_0^{\\infty} e^{-x} f(x) dx`. It will correctly integrate polynomials of degree :math:`2n - 1` or less
    over the interval :math:`[0, \\infty]` with weight function
    :math:`f(x) = x^{\alpha} e^{-x}`. See https://en.wikipedia.org/wiki/Gauss%E2%80%93Laguerre_quadrature.

    Examples
    --------
    >>> gl=torchquad.GaussLaguerre()
    >>> integral=gl.integrate(lambda x,a: np.sin(a*x),dim=1,N=20,args=(1,)) #integral from 0 to inf of np.exp(-x)*np.sin(x)
    |TQ-INFO| Computed integral was 0.49999999999998246. #analytic result = 0.5"""

    def __init__(self):
        super().__init__()
        self.name = "Gauss-Laguerre"
        self.root_fn = scipy.special.roots_laguerre
        self.default_integration_domain = [[0, numpy.inf]]
        self.wrapper_func = None

    def _get_constant_multiplier(self):
        """For the fixed integration domain of Gauss-Laguerre integration, the approximation can be calculated with no multiplicative factor."""
        return 1.0


class GaussHermite(Gaussian):
    """Gauss Hermite quadrature rule in torch, for integrals of the form :math:`\\int_{-\\infty}^{+\\infty} e^{-x^{2}} f(x) dx`. It will correctly integrate
    polynomials of degree :math:`2n - 1` or less over the interval
    :math:`[-\\infty, \\infty]` with weight function :math:`f(x) = e^{-x^2}`. See https://en.wikipedia.org/wiki/Gauss%E2%80%93Hermite_quadrature

    Examples
    --------
    >>> gh=torchquad.GaussHermite()
    >>> integral=gh.integrate(lambda x: 1-x,dim=1,N=200) #integral from -inf to inf of np.exp(-(x**2))*(1-x)
    |TQ-INFO| Computed integral was 1.7724538509055168. #analytic result = sqrt(pi)
    """

    def __init__(self):
        super().__init__()
        self.name = "Gauss-Hermite"
        self.root_fn = scipy.special.roots_hermite
        self.default_integration_domain = [[-1 * numpy.inf, numpy.inf]]
        self.wrapper_func = None

    def _get_constant_multiplier(self):
        """For the fixed integration domain of Gauss-Hermite integration, the approximation can be calculated with no multiplicative factor."""
        return 1.0
