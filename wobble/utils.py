# -*- coding: utf-8 -*-

from __future__ import division, print_function

__all__ = ["fit_continuum", "bin_data"]

import numpy as np


def fit_continuum(x, y, ivars, order=6, nsigma=[0.3,3.0], maxniter=50):
    """Fit the continuum using sigma clipping

    Args:
        x: The wavelengths
        y: The log-fluxes
        order: The polynomial order to use
        nsigma: The sigma clipping threshold: tuple (low, high)
        maxniter: The maximum number of iterations to do

    Returns:
        The value of the continuum at the wavelengths in x

    """
    A = np.vander(x - np.nanmean(x), order+1)
    m = np.ones(len(x), dtype=bool)
    for i in range(maxniter):
        m[ivars == 0] = 0  # mask out the bad pixels
        w = np.linalg.solve(np.dot(A[m].T, A[m]), np.dot(A[m].T, y[m]))
        mu = np.dot(A, w)
        resid = y - mu
        sigma = np.sqrt(np.nanmedian(resid**2))
        #m_new = np.abs(resid) < nsigma*sigma
        m_new = (resid > -nsigma[0]*sigma) & (resid < nsigma[1]*sigma)
        if m.sum() == m_new.sum():
            m = m_new
            break
        m = m_new
    return mu

def bin_data(xs, ys, xps):
    """
    Bin data onto a uniform grid using medians.
    
    Args:
        `xs`: `[N, M]` array of xs
        `ys`: `[N, M]` array of ys
        `xps`: `M'` grid of x-primes for output template
    
    Returns:
        `yps`: `M'` grid of y-primes
    
    """
    all_ys, all_xs = np.ravel(ys), np.ravel(xs)
    dx = xps[1] - xps[0] # ASSUMES UNIFORM GRID
    yps = np.zeros_like(xps)
    for i,t in enumerate(xps):
        ind = (all_xs >= t-dx/2.) & (all_xs < t+dx/2.)
        if np.sum(ind) > 0:
            yps[i] = np.nanmedian(all_ys[ind])
    ind_nan = np.isnan(yps)
    yps.flat[ind_nan] = np.interp(xps[ind_nan], xps[~ind_nan], yps[~ind_nan])
    return xps, yps