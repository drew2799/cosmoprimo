import functools
import logging

logging.getLogger('jax._src.lib.xla_bridge').addFilter(logging.Filter('No GPU/TPU found, falling back to CPU.'))


# jax array types
array_types = ()

import numpy as np

try:
    # raise ImportError
    import jax, jaxlib
    from jax import config
    config.update('jax_enable_x64', True)
    from jax import numpy, scipy
    from jax.tree_util import register_pytree_node_class
    array_types = []
    for line in ['jaxlib.xla_extension.DeviceArrayBase', 'type(numpy.array(0))', 'jax.core.Tracer']:
        try:
            array_types.append(eval(line))
        except AttributeError:
            pass
    array_types = tuple(array_types)
    from jax import vmap
except ImportError:
    jax = None
    import numpy
    import scipy
    vmap = numpy.vectorize
    def register_pytree_node_class(cls):
        return cls


def jit(*args, **kwargs):
    """Return :mod:`jax` just-in-time compiler."""

    def get_wrapper(func):
        if jax is None:
            return func
        return jax.jit(func, **kwargs)

    if kwargs or not args:
        return get_wrapper

    if len(args) != 1:
        raise ValueError('unexpected args: {}'.format(args))

    return get_wrapper(args[0])


def use_jax(*arrays):
    """Whether to use jax.numpy depending on whether array is jax's object."""
    return any(isinstance(array, array_types) for array in arrays)


def numpy_jax(*args, return_use_jax=False):
    """Return numpy or jax.numpy depending on whether array is jax's object."""
    uj = use_jax(*args)
    toret = numpy if uj else np
    if return_use_jax:
        return toret, uj
    return toret


def _interpax_convert_method(k):
    return  {1: 'linear', 3: 'cubic2'}[k]


def _scipy_convert_method(k):
    return  {1: 'linear', 3: 'cubic'}[k]


def _mask_bounds(x, xlim, bounds_error=False):
    jnp = numpy_jax(*x)
    masks = [(xx >= xxlim[0]) & (xx <= xxlim[1]) for xx, xxlim in zip(x, xlim)]

    if bounds_error:
        def raise_error():
            for mask, xx, xxlim in zip(masks, x, xlim):
                if not jnp.all(mask):
                    raise ValueError('input outside of extrapolation range (min: {} vs. {}; max: {} vs. {})'.format(xx.min(), xxlim[0], xx.max(), xxlim[1]))
        exception(raise_error)

    return masks


class Interpolator1D(object):

    """Wrapper for 1D interpolation; use :mod:`interpax` if :mod:`jax` input, else :func:`scipy.interpolate.interp1d`."""

    def __init__(self, x, fun, k=3, interp_x='lin', interp_fun='lin', extrap=False, assume_sorted=False):
        self._use_jax = use_jax(x, fun)
        self._np = numpy if self._use_jax else np
        self.interp_x = str(interp_x)
        self.interp_fun = str(interp_fun)
        x = self._np.array(x, dtype='f8')
        fun = self._np.array(fun, dtype='f8')
        self.shape = fun.shape[1:]
        if not assume_sorted:
            ix = self._np.argsort(x)
            x, fun = (xx[ix] for xx in (x, fun))
        self.xmin, self.xmax = x[0], x[-1]
        self._x, self._fun = x, fun
        if self.interp_x == 'log': x = self._np.log10(x)
        if self.interp_fun == 'log': fun = self._np.log10(fun)
        self.extrap = bool(extrap)
        self._mask_nan = None
        fun = fun.reshape(x.size, -1)
        if self._use_jax:
            from interpax import Interpolator1D
            self._spline = Interpolator1D(x, fun, method=_interpax_convert_method(k), extrap=self.extrap, period=None)
        else:
            from scipy import interpolate
            self._mask_nan = ~np.isnan(fun).all(axis=0)  # hack: scipy returns NaN for all shape[1] if any is NaN
            self._spline = interpolate.interp1d(x, fun[..., self._mask_nan], kind=_scipy_convert_method(k), axis=0, bounds_error=False, fill_value='extrapolate' if self.extrap else numpy.nan, assume_sorted=True)
            #from scipy.interpolate import CubicSpline#, UnivariateSpline
            #if k == 3: self._spline = CubicSpline(x, fun, axis=0, bc_type='natural', extrapolate=extrap)
            #else: self._spline = lambda t: numpy.interp(t, x, fun, period=None)

    def __call__(self, x, bounds_error=False):
        from .utils import _bcast_dtype
        dtype = _bcast_dtype(x)
        x = self._np.asarray(x, dtype=dtype)
        toret_shape = x.shape + self.shape
        x = x.ravel()
        mask_x, = _mask_bounds([x], [(self.xmin, self.xmax)], bounds_error=bounds_error)
        if self.interp_x == 'log': x = self._np.log10(x)
        tmp = self._spline(x)
        if self.interp_fun == 'log': tmp = 10**tmp
        toret = tmp = tmp if self.extrap else self._np.where(mask_x, tmp.T, self._np.nan).T
        if self._mask_nan is not None:
            toret = self._np.full((x.size, self._mask_nan.size), self._np.nan)
            toret[..., self._mask_nan] = tmp
        return toret.astype(dtype).reshape(toret_shape)


class Interpolator2D(object):

    """Wrapper for 2D interpolation; use :mod:`interpax` if :mod:`jax` input, else :func:`scipy.interpolate.interp1d`."""

    def __init__(self, x, y, fun, kx=3, ky=3, interp_x='lin', interp_y='lin', interp_fun='lin', extrap=False, assume_sorted=False):
        self._use_jax = use_jax(x, y, fun)
        self._np = numpy if self._use_jax else np
        self.interp_x = str(interp_x)
        self.interp_y = str(interp_y)
        self.interp_fun = str(interp_fun)
        x, y = (self._np.array(xx, dtype='f8') for xx in (x, y))
        fun = self._np.array(fun, dtype='f8')
        if not assume_sorted:
            ix, iy = (self._np.argsort(xx) for xx in (x, y))
            x, y, fun = x[ix], y[iy], fun[self._np.ix_(ix, iy)]
        self.xmin, self.xmax = x[0], x[-1]
        self.ymin, self.ymax = y[0], y[-1]
        self._x, self._y, self._fun = x, y, fun
        if self.interp_x == 'log': x = self._np.log10(x)
        if self.interp_y == 'log': y = self._np.log10(y)
        if self.interp_fun == 'log': fun = self._np.log10(fun)
        self.extrap = bool(extrap)
        if self._use_jax:
            from interpax import Interpolator2D
            methodx = _interpax_convert_method(kx)
            methody = _interpax_convert_method(ky)
            assert methody == methodx, 'interpax supports ky = ky only'
            self._spline = Interpolator2D(x, y, fun, method=methodx, extrap=self.extrap, period=None)
        else:
            from scipy.interpolate import RectBivariateSpline
            self._spline = RectBivariateSpline(x, y, fun, kx=kx, ky=ky, s=0)

    def __call__(self, x, y, grid=True, bounds_error=False):
        from .utils import _bcast_dtype
        dtype = _bcast_dtype(x, y)
        x, y = (self._np.asarray(xx, dtype=dtype) for xx in (x, y))
        if grid:
            toret_shape = x.shape + y.shape
        else:
            toret_shape = x.shape
        x, y = (xx.ravel() for xx in (x, y))
        mask_x, mask_y = _mask_bounds([x, y], [(self.xmin, self.xmax), (self.ymin, self.ymax)], bounds_error=bounds_error)
        if grid: mask_x = mask_x[:, None] & mask_y
        else: mask_x = mask_x & mask_y
        if self.interp_x == 'log': x = self._np.log10(x)
        if self.interp_y == 'log': y = self._np.log10(y)
        if self._use_jax:
            _shape = (x.size, y.size)
            if grid:
                x, y = self._np.meshgrid(x, y, indexing='ij')
                tmp = self._spline(x.ravel(), y.ravel()).reshape(_shape)
            else:
                tmp = self._spline(x, y)
        else:
            if grid:
                i_x = self._np.argsort(x)
                i_y = self._np.argsort(y)
                tmp = self._spline(x[i_x], y[i_y], grid=True)[self._np.ix_(self._np.argsort(i_x), self._np.argsort(i_y))]
            else:
                tmp = self._spline(x, y, grid=False)
        if self.interp_fun == 'log': tmp = 10**tmp
        toret = tmp if self.extrap else self._np.where(mask_x, tmp, self._np.nan)
        return toret.astype(dtype).reshape(toret_shape)


def scan_numpy(f, init, xs, length=None):
    if xs is None:
        xs = [None] * length
    carry = init
    ys = []
    for x in xs:
        carry, y = f(carry, x)
        ys.append(y)
    return carry, numpy.stack(ys)


def for_cond_loop_numpy(lower, upper, cond_fun, body_fun, init_val):
    val = init_val
    for i in range(lower, upper):
        if not cond_fun(i, val): break
        val = body_fun(i, val)
    return val


def switch_numpy(index, branches, *operands):
    return branches[index](*operands)


def select_numpy(pred, on_true, on_false):
    if pred: return on_true
    return on_false


def cond_numpy(pred, true_fun, false_fun, *operands):
    if pred:
        return true_fun(*operands)
    return false_fun(*operands)


def exception_numpy(fun, *args):
    return fun(*args)


def opmask(array, mask, value, op='set'):
    if use_jax(array):
        if op == 'set':
            return array.at[mask].set(value)
        if op == 'add':
            return array.at[mask].add(value)
    else:
        if op == 'set':
            array[mask] = value
            return array
        if op == 'add':
            array[mask] += value
            return array


if jax is None:

    scan = scan_numpy
    switch = switch_numpy
    select = select_numpy
    cond = cond_numpy
    exception = exception_numpy
    for_cond_loop = for_cond_loop_numpy

else:

    scan = jax.lax.scan
    switch = jax.lax.switch
    select = jax.lax.select
    cond = jax.lax.cond
    exception = jax.debug.callback
    select = jax.lax.select

    def for_cond_loop(lower, upper, cond_fun, body_fun, init_val, **kwargs):

        def body(i, val):
            return jax.lax.cond(cond_fun(i, val), body_fun, lambda i, val: val, i, val)

        return jax.lax.fori_loop(lower, upper, body, init_val, **kwargs)


def simpson(y, x=None, dx=1, axis=-1, even='avg'):
    """
    Taken from :mod:`scipy.integrate`, https://github.com/scipy/scipy/blob/v1.0.0/scipy/integrate/quadrature.py#L332-L436.

    Integrate y(x) using samples along the given axis and the composite
    Simpson's rule.  If x is None, spacing of dx is assumed.

    If there are an even number of samples, N, then there are an odd
    number of intervals (N-1), but Simpson's rule requires an even number
    of intervals.  The parameter 'even' controls how this is handled.

    Parameters
    ----------
    y : array_like
        Array to be integrated.
    x : array_like, optional
        If given, the points at which `y` is sampled.
    dx : int, optional
        Spacing of integration points along axis of `y`. Only used when
        `x` is None. Default is 1.
    axis : int, optional
        Axis along which to integrate. Default is the last axis.
    even : str {'avg', 'first', 'last'}, optional
        'avg' : Average two results:1) use the first N-2 intervals with
                  a trapezoidal rule on the last interval and 2) use the last
                  N-2 intervals with a trapezoidal rule on the first interval.

        'first' : Use Simpson's rule for the first N-2 intervals with
                a trapezoidal rule on the last interval.

        'last' : Use Simpson's rule for the last N-2 intervals with a
               trapezoidal rule on the first interval.

    See Also
    --------
    quad: adaptive quadrature using QUADPACK
    romberg: adaptive Romberg quadrature
    quadrature: adaptive Gaussian quadrature
    fixed_quad: fixed-order Gaussian quadrature
    dblquad: double integrals
    tplquad: triple integrals
    romb: integrators for sampled data
    cumtrapz: cumulative integration for sampled data
    ode: ODE integrators
    odeint: ODE integrators

    Notes
    -----
    For an odd number of samples that are equally spaced the result is
    exact if the function is a polynomial of order 3 or less.  If
    the samples are not equally spaced, then the result is exact only
    if the function is a polynomial of order 2 or less.

    """
    y = numpy.asarray(y)
    nd = len(y.shape)
    N = y.shape[axis]
    last_dx = dx
    first_dx = dx
    returnshape = 0

    def tupleset(t, i, value):
        l = list(t)
        l[i] = value
        return tuple(l)

    def _basic_simpson(y, start, stop, x, dx, axis):
        nd = len(y.shape)
        if start is None:
            start = 0
        step = 2

        slice_all = (slice(None),) * nd
        slice0 = tupleset(slice_all, axis, slice(start, stop, step))
        slice1 = tupleset(slice_all, axis, slice(start + 1, stop + 1, step))
        slice2 = tupleset(slice_all, axis, slice(start + 2, stop + 2, step))

        if x is None:  # Even spaced Simpson's rule.
            result = numpy.sum(dx / 3.0 * (y[slice0] + 4 * y[slice1] + y[slice2]), axis=axis)
        else:
            # Account for possibly different spacings.
            #    Simpson's rule changes a bit.
            h = numpy.diff(x, axis=axis)
            sl0 = tupleset(slice_all, axis, slice(start, stop, step))
            sl1 = tupleset(slice_all, axis, slice(start + 1, stop + 1, step))
            h0 = h[sl0]
            h1 = h[sl1]
            hsum = h0 + h1
            hprod = h0 * h1
            h0divh1 = h0 / h1
            tmp = hsum / 6.0 * (y[slice0] * (2 - 1.0 / h0divh1) +
                                y[slice1] * hsum * hsum / hprod +
                                y[slice2] * (2 - h0divh1))
            result = numpy.sum(tmp, axis=axis)
        return result

    if x is not None:
        x = numpy.asarray(x)
        if len(x.shape) == 1:
            shapex = [1] * nd
            shapex[axis] = x.shape[0]
            saveshape = x.shape
            returnshape = 1
            x = x.reshape(tuple(shapex))
        elif len(x.shape) != len(y.shape):
            raise ValueError("If given, shape of x must be 1-d or the "
                             "same as y.")
        if x.shape[axis] != N:
            raise ValueError("If given, length of x along axis must be the "
                             "same as y.")
    if N % 2 == 0:
        val = 0.0
        result = 0.0
        slice1 = (slice(None),)*nd
        slice2 = (slice(None),)*nd
        if even not in ['avg', 'last', 'first']:
            raise ValueError("Parameter 'even' must be "
                             "'avg', 'last', or 'first'.")
        # Compute using Simpson's rule on first intervals
        if even in ['avg', 'first']:
            slice1 = tupleset(slice1, axis, -1)
            slice2 = tupleset(slice2, axis, -2)
            if x is not None:
                last_dx = x[slice1] - x[slice2]
            val += 0.5 * last_dx * (y[slice1] + y[slice2])
            result = _basic_simpson(y, 0, N - 3, x, dx, axis)
        # Compute using Simpson's rule on last set of intervals
        if even in ['avg', 'last']:
            slice1 = tupleset(slice1, axis, 0)
            slice2 = tupleset(slice2, axis, 1)
            if x is not None:
                first_dx = x[tuple(slice2)] - x[tuple(slice1)]
            val += 0.5 * first_dx * (y[slice2] + y[slice1])
            result += _basic_simpson(y, 1, N - 2, x, dx, axis)
        if even == 'avg':
            val /= 2.0
            result /= 2.0
        result = result + val
    else:
        result = _basic_simpson(y, 0, N - 2, x, dx, axis)
    if returnshape:
        x = x.reshape(saveshape)
    return result


def romberg(function, a, b, args=(), epsabs=1e-8, epsrel=1e-8, divmax=10, return_error=False):
    """
    Romberg integration of a callable function or method.

    .. deprecated:: 1.12.0

        This function is deprecated as of SciPy 1.12.0 and will be removed
        in SciPy 1.15.0. Please use `scipy.integrate.quad` instead.

    Returns the integral of `function` (a function of one variable)
    over the interval (`a`, `b`).

    If `show` is 1, the triangular array of the intermediate results
    will be printed. If `vec_func` is True (default is False), then
    `function` is assumed to support vector arguments.

    Parameters
    ----------
    function : callable
        Function to be integrated.
    a : float
        Lower limit of integration.
    b : float
        Upper limit of integration.

    Returns
    -------
    results : float
        Result of the integration.

    Other Parameters
    ----------------
    args : tuple, optional
        Extra arguments to pass to function. Each element of `args` will
        be passed as a single argument to `func`. Default is to pass no
        extra arguments.
    divmax : int, optional
        Maximum order of extrapolation. Default is 10.

    See Also
    --------
    fixed_quad : Fixed-order Gaussian quadrature.
    quad : Adaptive quadrature using QUADPACK.
    dblquad : Double integrals.
    tplquad : Triple integrals.
    romb : Integrators for sampled data.
    simpson : Integrators for sampled data.
    cumulative_trapezoid : Cumulative integration for sampled data.

    References
    ----------
    .. [1] 'Romberg's method' https://en.wikipedia.org/wiki/Romberg%27s_method

    Examples
    --------
    Integrate a gaussian from 0 to 1 and compare to the error function.

    >>> from scipy import integrate
    >>> from scipy.special import erf
    >>> import numpy as np
    >>> gaussian = lambda x: 1/np.sqrt(np.pi) * np.exp(-x**2)
    >>> result = integrate.romberg(gaussian, 0, 1, show=True)
    Romberg integration of <function vfunc at ...> from [0, 1]

    ::

    Steps  StepSize  Results
        1  1.000000  0.385872
        2  0.500000  0.412631  0.421551
        4  0.250000  0.419184  0.421368  0.421356
        8  0.125000  0.420810  0.421352  0.421350  0.421350
        16  0.062500  0.421215  0.421350  0.421350  0.421350  0.421350
        32  0.031250  0.421317  0.421350  0.421350  0.421350  0.421350  0.421350

    The final result is 0.421350396475 after 33 function evaluations.

    >>> print("%g %g" % (2*result, erf(1)))
    0.842701 0.842701

    """
    def _difftrap(function, interval, numtraps):
        """
        Perform part of the trapezoidal rule to integrate a function.
        Assume that we had called difftrap with all lower powers-of-2
        starting with 1. Calling difftrap only returns the summation
        of the new ordinates. It does _not_ multiply by the width
        of the trapezoids. This must be performed by the caller.
            'function' is the function to evaluate (must accept vector arguments).
            'interval' is a sequence with lower and upper limits
                    of integration.
            'numtraps' is the number of trapezoids to use (must be a
                    power-of-2).
        """
        numtosum = numtraps // 2
        h = (interval[1] - interval[0]) * 1. / numtosum
        lox = interval[0] + 0.5 * h
        points = lox + h * numpy.arange(numtosum)
        s = numpy.sum(function(points), axis=0)
        return s

    def _romberg_diff(b, c, k):
        """
        Compute the differences for the Romberg quadrature corrections.
        See Forman Acton's "Real Computing Made Real," p 143.
        """
        tmp = 4.0**k
        return (tmp * c - b) / (tmp - 1.0)

    vfunc = lambda x: function(x, *args)
    n = 1
    interval = [a, b]
    intrange = b - a
    ordsum = 0.5 * (vfunc(interval[0]) + vfunc(interval[1]))

    if use_jax(ordsum):
        from jax import numpy
        scan = jax.lax.scan
    else:
        import numpy
        scan = scan_numpy


    result = intrange * ordsum
    last_row = numpy.array([result] * (divmax + 1))
    err = numpy.inf

    def scan_fun(carry, y):
        x, k = carry
        x = _romberg_diff(y, x, k + 1)
        return (x, k + 1), x

    last_row = numpy.array([result])

    for i in range(1, divmax + 1):
        n *= 2
        ordsum += _difftrap(vfunc, interval, n)
        x = intrange * ordsum / n
        _, row = scan(scan_fun, (x, 0), last_row[:i])
        row = numpy.concatenate([x[None, :], row])
        err = numpy.abs(last_row[i - 1] - row[i])
        last_row = row

    result = last_row[i]

    def raise_error(err, result):
        if not (numpy.all(err < epsabs) or numpy.all(err < numpy.abs(result) * epsrel)):
            raise ValueError('precision not achieved')

    exception(raise_error, err, result)
    if return_error:
        return result, err
    return result


def odeint(func, y0, t, args=(), method='rk4'):

    t = numpy.asarray(t)
    shape = t.shape
    t = t.ravel()

    func = lambda y, t: func(y, t, *args)

    if method == 'rk1':

        def integrator(carry, t):
            y, t_last = carry
            h = t - t_last
            k1 = func(y, t_last)
            y = y + h * k1
            return (y, t), y

    if method == 'rk2':

        def integrator(carry, t):
            y, t_last = carry
            h = t - t_last
            k1 = func(y, t_last)
            k2 = func(y + h * k1 / 2, t_last + h / 2)
            y = y + h * k2
            return (y, t), y

    if method == 'rk4':

        def integrator(carry, t):
            y, t_last = carry
            h = t - t_last
            k1 = func(y, t_last)
            k2 = func(y + h * k1 / 2, t_last + h / 2)
            k3 = func(y + h * k2 / 2, t_last + h / 2)
            k4 = func(y + h * k3, t)
            y = y + h / 6. * (k1 + 2 * k2 + 2 * k3 + k4)
            return (y, t), y

    toret = scan(integrator, (y0, t[0]), t)[1]
    if not shape: toret = toret[0]
    return toret.reshape(shape)