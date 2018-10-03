#!/usr/bin/env python
"""Provide base classes for problem and solver implementations."""
from statistics import mean, variance
from math import sqrt, ceil, floor
from .prng.mrg32k3a import get_next_prnstream
import multiprocessing as mp
from copy import deepcopy
from .chnutils import perturb, argsort, enorm, get_setnbors, get_nbors, is_lwep, get_nondom, does_strict_dominate, does_weak_dominate, does_dominate


class MOSOSolver(object):
    """Base class for solver implentations."""

    def __init__(self, orc):
        """Initialize a solver object by assigning an oracle problem."""
        self.orc = orc
        self.num_calls = 0
        self.num_obj = self.orc.num_obj
        self.dim = self.orc.dim
        super().__init__()


class RASolver(MOSOSolver):
    """Implementation of a MOSO solver based on RLE."""
    def __init__(self, orc, **kwargs):
        self.nbor_rad = kwargs.pop('nbor_rad', 1)
        try:
            self.sprn = kwargs.pop('sprn')
            self.x0 = kwargs.pop('x0')
        except KeyError:
            print(' -- -- KeyError: Please specify an x0 and a random number seed for the solver.')
        super().__init__(orc)

    def solve(self, budget):
        """Initialize and solve a MOSO problem."""
        self.orc.set_crnflag(True)
        seed1 = self.orc.rng._current_seed
        lesnu = dict()
        simcalls = dict()
        lesnu[0] = set() | {self.x0}
        simcalls[0] = 0
        # initialize the iteration counter
        self.nu = 0
        # invoke the Retrospective approximation algorithm
        self.rasolve(lesnu, simcalls, budget)
        # name the data keys and return the results
        final_ind = len(lesnu) - 1
        final_les = lesnu[final_ind]
        return final_les

    def spline(self, x0, e=float('inf'), nobj=0, kcon=0):
        """
        return an estimated local minimizer using pseudo-gradients

        Keyword Arguments:
        x0 -- feasible starting point, tuple of length self.dim
        fx0 -- estimate of x0, tuple of length self.num_obj
        sex0 -- standard error of x0, tuple of length self.num_obj
        e -- objective function constraint, scalar real number
            or float('inf') for unconstrained
        nobj -- objective to minimize, natural number < self.num_obj
        kcon -- objective to constrain, natural number < self.num_obj

        Return Values:
        mcs -- search trajectory of SPLI, set of point tuples
        xn -- estimated local minimizer, tuple of length self.dim
        fxn -- estimate of xn, tuple of length self.num_obj
        sexn -- standard error of xn, tuple of length self.num_obj

        See Wang et. al 2013, R-SPLINE.
        """
        fx0 = self.gbar[x0]
        sex0 = self.sehat[x0]
        b = self.b
        bp = 0
        xn = x0
        fxn = fx0
        sexn = sex0
        mcT = set()
        should_stop = False
        while not should_stop:
            xs, fxs, sexs, np = self.spli(xn, fxn, sexn, e, nobj, kcon, b)
            mcT |= {xs}
            xn, fxn, sexn, npp = self.ne(xs, fxs, sexs, nobj, e, kcon)
            mcT |= {xn}
            bp += np + npp
            if bp >= b or fxn[nobj] == fxs[nobj]:
                should_stop = True
        return mcT, xn, fxn, sexn

    def ne(self, x, fx, sex, nobj, e=float('inf'), kcon=0):
        """
        return the minimizer of the neighborhood of x

        Keyword Arguments:
        x -- a candidate local minimizer, tuple of length self.dim
        fx -- estimate of x on each objective, tuple of length self.num_obj
        sex -- standard error of x, tuple of length self.num_obj
        nobj -- objective to minimize, natural number < self.num_obj
        e -- objective function constraint, scalar real number
            or float('inf') for unconstrained (default)
        kcon -- objective to constrain, natural number < self.num_obj

        Return Values:
        xs -- estimated local minimizer, tuple of length self.dim
        fxs -- objective values of xs, tuple of length self.num_obj
        sexs -- standard errors of xs, tuple of length self.num_obj

        See Wang et. al 2013, R-SPLINE.
        """
        q = self.dim
        m = self.m
        n = 0
        xs = x
        fxs = fx
        vxs = sex
        nbor_rad = self.nbor_rad
        # optimize the case for neighborhood radius of 1
        if nbor_rad == 1:
            for i in range(q):
                xp1 = tuple(x[j] + 1 if i == j else x[j] for j in range(q))
                xm1 = tuple(x[j] - 1 if i == j else x[j] for j in range(q))
                isfeas1, fxp1, vxp1 = self.estimate(xp1, e, kcon)
                if isfeas1:
                    n += m
                    if fxp1[nobj] < fxs[nobj]:
                        xs = xp1
                        fxs = fxp1
                        vxs = vxp1
                        return xs, fxs, vxs, n
                isfeas2, fxm1, vxm1 = self.estimate(xm1, e, kcon)
                if isfeas2:
                    n += m
                    if fxm1[nobj] < fxs[nobj]:
                        xs = xm1
                        fxs = fxm1
                        vxs = vxm1
                        return xs, fxs, vxs, n
        else:
            # for neighborhoods not 1, generate the list of neighbors
            nbors = get_nbors(x, nbor_rad)
            # and check each neighbor until we find a better one
            for nb in nbors:
                isfeas, fn, sen = self.estimate(nb, e, kcon)
                if isfeas:
                    n += m
                    if fn[nobj] < fxs[nobj]:
                        xs = nb
                        fxs = fn
                        vxs = sen
                        break
        return xs, fxs, vxs, n

    def pli(self, x, nobj):
        """
        return a search direction for seeking a local minimizer

        Keyword Arguments:
        x -- a feasible starting point, tuple of length self.dim
        nobj -- objective to minimize, natural number < self.num_obj

        Return Values:
        gamma -- gradient at perturbed x
        gbat -- estimated interpolated function value at perturbed x

        See Wang et. al 2013, R-SPLINE.
        """
        q = self.dim
        x0 = tuple(floor(x[i]) for i in range(q))
        simp = [x0]
        zi = [x[i] - x0[i] for i in range(q)]
        zi.extend((0, 1))
        p = argsort(zi)
        p.reverse()
        z = sorted(zi, reverse=True)
        w = tuple(z[i] - z[i + 1] for i in range(q + 1))
        prevx = x0
        for i in range(1,q + 1):
            x1 = tuple(prevx[j] + 1 if j == p[i] else prevx[j] for j in range(q))
            simp.append(x1)
            prevx = x1
        n = 0
        t = 0
        gbat = 0
        ghat = {}
        xbest = None
        fxbest = None
        for i in range(q + 1):
            isfeas, fx, vx = self.estimate(simp[i])
            if isfeas:
                if not xbest:
                    xbest = simp[i]
                    fxbest = fx
                n += 1
                t += w[i]
                gbat += w[i]*fx[nobj]
                ghat[simp[i]] = fx
                if fx[nobj] < fxbest[nobj]:
                    xbest = simp[i]
                    fxbest = fx
        if t > 0:
            gbat /= t
        else:
            gbat = float('inf')
        if n < q + 1:
            gamma = None
        else:
            gamma = [0]*q
            for i in range(1, q + 1):
                gamma[p[i]] = ghat[simp[i]][nobj] - ghat[simp[i - 1]][nobj]
        return gamma, gbat, xbest, fxbest

    def spli(self, x0, fx0, sex0, e, nobj, kcon, b):
        """
        return a candidate minimizer by following a search direction

        Keyword Arguments:
        x0 -- a feasible starting point, tuple of length self.dim
        fx0 -- estimate of x0 on each objective, tuple of length self.num_obj
        sex0 -- standard error of x0, tuple of length self.num_obj
        e -- value to constrain the feasible space, scalar real number
            or float('inf') for unconstrained
        nobj -- the objective to minimize, a natural number < self.num_obj
        kcon -- the objective to constrain, a natural number < self.num_obj

        Return Values:
        xs -- a candidate local minimizer, tuple of length self.dim
        fxs -- estimate of xs, tuple of length self.num_obj
        sexs -- standard error of xs, tuple of length self.dim

        See Wang et. al 2013, R-SPLINE.
        """
        sprn = self.sprn
        m = self.m
        q = len(x0)
        ss = 2.0
        xs = x0
        fxs = fx0
        sexs = sex0
        n = 0
        c = 2.0
        stop_loop = False
        while not stop_loop:
            x1 = perturb(x0, sprn)
            gamma, gbat, xbest, fxbest = self.pli(x1, nobj)
            if fxbest[nobj] < fxs[nobj]:
                xs = xbest
                fxs = fxbest
            n += m*(q + 1)
            if not gamma or gamma == [0.0]*q:
                stop_loop = True
                break
            if n > b:
                stop_loop = True
                break
            i = 0
            x0 = xs
            should_stop = False
            while not should_stop:
                i += 1
                s = ss*pow(c, i - 1)
                x1 = tuple(int(floor(x0[j] - s*gamma[j]/enorm(gamma))) for j in range(q))
                isfeas, fx1, sex1 = self.estimate(x1, e, kcon)
                if isfeas:
                    n += m
                    if fx1[nobj] < fxs[nobj]:
                        xs = x1
                        fxs = fx1
                        sexs = sex1
                if not x1 == xs:
                    should_stop = True
                x0 = xs
            if i <= 2:
                stop_loop = True
        return xs, fxs, sexs, n

    def estimate(self, x, con=float('inf'), nobj=0):
        """
        return esimates of g(x) while checking objective feasibility

        Keyword Arguments:
        x -- the vector, or system, to estimate
        con -- the constraint on the objective nobj, default to unconstrained
        nobj -- the objective to constrain, default to 0 (arbitrary)

        Return Values:
        isfeas -- boolean indicating feasibility
        fx -- tuple of estimates of each objective
        vx -- tuple of standard error of each objective

        The object should ensure self.m is set as the sample size,
        self.gbar is a dictionary of objective values, and self.sehat is
        a dictionary of standard errors.
        """
        m = self.m
        #first, check if x has already been sampled in this iteration
        if x in self.gbar:
            isfeas = True
            fx = self.gbar[x]
            vx = self.sehat[x]
        #if not, perform sampling
        else:
            isfeas, fx, vx = self.orc.hit(x, m)
            if isfeas:
                self.gbar[x] = fx
                self.sehat[x] = vx
        #next, check feasibility against the constraint which may be different
        # than oracle feasibility
        if isfeas:
            self.num_calls += m
            if fx[nobj] > con:
                isfeas = False
        return isfeas, fx, vx

    def rasolve(self, phatnu, simcalls, budget):
        """Repeatedly call a deterministic solver at increasing sample size."""
        while self.num_calls < budget:
            self.nu += 1
            self.m = self.calc_m(self.nu)
            self.b = self.calc_b(self.nu)
            self.gbar = dict()
            self.sehat = dict()
            aold = phatnu[self.nu - 1]
            phatnu[self.nu] = self.spsolve(aold)
            simcalls[self.nu] = self.num_calls
            self.orc.crn_advance()

    def spsolve(self, warm_start):
        """Solve a sample path problem. Implement this in the child class."""
        pass

    def upsample(self, mcS):
        """sample a set at the current sample size"""
        outset = set()
        for s in mcS:
            isfeas, fs, ses = self.estimate(s)
            if isfeas:
                outset |= {s}
        return outset

    def calc_m(self, nu):
        """return the sample size for an iteration nu, as in rspline"""
        mmul = 1.1
        m_init = 2
        return ceil(m_init*pow(mmul, nu))

    def calc_b(self, nu):
        """return the limit on spline calls for an iteration nu"""
        mmul = 1.2
        m_init = 8*(self.dim - 1)
        return ceil(m_init*pow(mmul, nu))


class RLESolver(RASolver):
    def __init__(self, orc, **kwargs):
        self.betadel = kwargs.pop('betadel', 0.5)
        super().__init__(orc, **kwargs)

    def spsolve(self, warm_start):
        """Search and then certify an ALES."""
        anew = self.accel(warm_start)
        ales = self.rle(anew)
        return ales

    def accel(self, warm_start):
        """Accelerate RLE - Implement this function in a child class."""
        return warm_start

    def rle(self, mcS):
        """Return a sample path ALES."""
        mcXw = {self.x0}
        mcS = self.upsample(mcS | mcXw)
        b = self.b
        n = 0
        tmp = {s: self.gbar[s] for s in mcS | mcXw}
        mcS = get_nondom(tmp)
        mcNnc = self.get_ncn(mcS)
        while n <= b and mcNnc:
            old_calls = self.num_calls
            mcNw, mcNd = self.remove_nlwep(mcNnc)
            mcNd -= mcS
            rlwepcalls = self.num_calls - old_calls
            mcS |= mcNw
            if not mcNw:
                mcXw = self.seek_lwep(mcNd, mcS)
                mcS |= mcXw
            tmp = {s: self.gbar[s] for s in mcS | {self.x0}}
            mcS = get_nondom(tmp)
            old_calls = self.num_calls
            mcNnc = self.get_ncn(mcS)
            ncncalls = self.num_calls - old_calls
            n += rlwepcalls + ncncalls
        return mcS

    def get_ncn(self, mcS):
        """Generate the Non-Conforming neighborhood of a candidate LES."""
        # initialize the non-conforming neighborhood
        ncn = set()
        d = self.num_obj
        r = self.nbor_rad
        dr = range(d)
        delN = get_setnbors(mcS, r)
        delzero = tuple(0 for i in dr)
        # defintion 9 (a) -- check for strict domination in the deleted nbors
        for s in mcS:
            fs = self.gbar[s]
            ses = self.sehat[s]
            dels = tuple(self.calc_delta(ses[i]) for i in dr)
            snb = get_nbors(s, r) - mcS
            for x in snb:
                isfeas, fx, sex = self.estimate(x)
                if isfeas:
                    delx = tuple(self.calc_delta(sex[i]) for i in dr)
                    if does_strict_dominate(fx, fs, delzero, delzero):
                        ncn |= {x}
        # definition 9 (b) initialization
        for x in delN - ncn:
            isfeas, fx, sex = self.estimate(x)
            if isfeas:
                # definition 9 (b) (i) initialization
                notweakdom = True
                # definition 9 (b) (ii) initialization
                notrelaxdom = True
                # definition 9 (b) (iii) initialization
                wouldnotchange = True
                doesweakdom = False
                # set the relaxation of the neighbor
                delx = tuple(self.calc_delta(sex[i]) for i in dr)
                for s in mcS:
                    fs = self.gbar[s]
                    ses = self.sehat[s]
                    # set the relaxation of the LES candidate member
                    dels = tuple(self.calc_delta(ses[i]) for i in dr)
                    # definition 9 (b) (i)
                    if does_weak_dominate(fs, fx, delzero, delzero):
                        notweakdom = False
                    # definition 9 (b) (ii)
                    if does_dominate(fx, fs, delzero, delzero) and does_dominate(fs, fx, dels, delx):
                        notrelaxdom = False
                    # definition 9 (b) (iii)
                    if does_weak_dominate(fx, fs, delzero, delzero):
                        doesweakdom = True
                    if does_weak_dominate(fs, fx, dels, delx) or does_weak_dominate(fx, fs, delx, dels):
                        wouldnotchange = False
                # definition 9 (b)
                if notweakdom and notrelaxdom and (wouldnotchange or doesweakdom):
                    ncn |= {x}
        return ncn

    def seek_lwep(self, mcNd, mcS):
        """Find a sample path LWEP."""
        b = self.b
        n = 0
        delz = [0]*self.num_obj
        mcXw = set()
        xnew = set() | mcNd
        while not mcXw and n <= b:
            old_calls = self.num_calls
            mcXw, mcXd = self.remove_nlwep(xnew)
            xnew = set([x for x in mcXd])
            n += self.num_calls - old_calls
        if not mcXw:
            mcXw |= xnew
        return mcXw

    def remove_nlwep(self, mcS):
        """Compute the subset of mcS that are not LWEPs."""
        r = self.nbor_rad
        lwepset = set()
        domset = set()
        delz = [0]*self.num_obj
        nbors = get_setnbors(mcS, r)
        nbors = self.upsample(nbors)
        tmpd = {x: self.gbar[x] for x in mcS | nbors}
        for s in mcS:
            islwep, dompts = is_lwep(s, r, tmpd)
            if islwep:
                lwepset |= {s}
            else:
                domset |= dompts
        return lwepset, domset

    def calc_delta(self, se):
        """Compute RLE relaxation for an iteration nu."""
        m = self.m
        relax = se/pow(m, self.betadel)
        return relax


class OrcBase(object):
    """Base class for problem implementations."""

    def check_xfeas(self, x):
        """Check if x is in the feasible domain."""
        is_feas = True
        qx = len(x)
        qo = self.dim
        if not qx == qo:
            return False
        mcD = self.get_feasspace()
        i = 0
        while i < len(mcD) and is_feas == True:
            comp_feas = False
            j = 0
            while j < len(mcD[i]) and comp_feas == False:
                if x[i] >= mcD[i][j][0] and x[i] < mcD[i][j][1]:
                    comp_feas = True
                j += 1
            if not comp_feas:
                is_feas = False
            i += 1
        return is_feas


class Oracle(OrcBase):
    """Base class for implementing problems with noise."""

    def __init__(self, rng):
        """Initialize a problem with noise with a pseudo-random generator."""
        self.rng = rng
        self.num_calls = 0
        self.set_crnflag(True)
        self.simpar = 1
        self.num_rand = 0
        super().__init__()

    def set_crnflag(self, crnflag):
        """Set the common random number (crn) flag and intialize the crn states."""
        self.crnflag = crnflag
        self.crnold_state = self.rng.getstate()
        self.crnnew_state = self.rng.getstate()

    def set_crnold(self, old_state):
        """Set the current crn rewind state."""
        self.crnold_state = old_state

    def set_crnnew(self, new_state):
        """Jump forward to start a new realization of crn."""
        self.crnnew_state = new_state

    def crn_reset(self):
        """Rewind to the first crn."""
        crn_state = self.crnold_state
        self.rng.setstate(crn_state)

    def crn_advance(self):
        """Jump ahead to the new crn, and set the new rewind point."""
        self.num_calls = 0
        crn_state = self.crnnew_state
        self.crnold_state = self.crnnew_state
        self.rng.setstate(crn_state)

    def crn_check(self, num_calls):
        """Rewind the rng if crnflag is True and set farthest CRN point."""
        if num_calls > self.num_calls:
            self.num_calls = num_calls
            prnstate = self.rng.getstate()
            self.set_crnnew(prnstate)
        if self.crnflag:
            self.crn_reset()

    def hit(self, x, m):
        """Generate the mean of spending m simulation effort at point x.

        Positional Arguments:
        x -- point to generate estimates
        m -- number of estimates to generate at x

        Return Values:
        isfeas -- boolean indicating feasibility of x
        omean -- mean of m estimates of each objective at x (tuple)
        ose -- mean of m estimates of the standard error of each objective
            at x (tuple)
        """
        d = self.num_obj
        dr = range(d)
        isfeas = False
        obmean = []
        obse = []
        mr = range(m)
        if m > 0:
            if m == 1:
                isfeas, objd = self.g(x, self.rng)
                obmean = objd
                obse = [0 for o in objd]
            else:
                if self.simpar == 1:
                    ## do not parallelize replications
                    feas = []
                    objm = []
                    for i in mr:
                        isfeas, objd = self.g(x, self.rng)
                        feas.append(isfeas)
                        objm.append(objd)
                    if all(feas):
                        isfeas = True
                        obmean = tuple([mean([objm[i][k] for i in mr]) for k in dr])
                        obvar = [variance([objm[i][k] for i in mr], obmean[k]) for k in dr]
                        obse = tuple([sqrt(obvar[i]/m) for i in dr])
                else:
                    sim_old = self.simpar
                    ## obtain replications in parallel
                    ## divide m into chunks for the processors
                    nproc = self.simpar
                    if self.simpar > m:
                        nproc = m
                    pr = range(nproc)
                    num_rands = [int(m/nproc) for i in pr]
                    for i in range(m % nproc):
                        num_rands[i] += 1
                    ## create prn for each process by jumping ahead 2^127 spots
                    ## and a hit function for each using an oracle object
                    start_seed = self.rng.get_seed()
                    ## turn off simpar during parallelization
                    self.simpar = 1
                    orclst = [self]
                    for i in range(len(num_rands) - 1):
                        nextprn = get_next_prnstream(start_seed)
                        start_seed = nextprn.get_seed()
                        myorc = deepcopy(self)
                        myorc.rng = nextprn
                        orclst.append(myorc)
                    ## take the replications in parallel
                    pres = []
                    feas = []
                    means = []
                    ses = []
                    with mp.Pool(nproc) as p:
                        for i, r in enumerate(num_rands):
                            pres.append(p.apply_async(orclst[i].hit, args=(x, r)))
                        for i in pr:
                            ## 0 = feas, 1 = mean, 2 = se
                            res = pres[i].get()
                            feas.append(res[0])
                            means.append(res[1])
                            ses.append(res[2])
                    ## turn simpar back on before returning
                    self.simpar = sim_old
                    if all(feas):
                        isfeas = True
                        ## weighted average of replications
                        obmean = tuple([sum([means[i][k]*num_rands[i]/m for i in pr]) for k in dr])
                        ### convert se output back to variance
                        obvar = [[num_rands[i]*ses[i][k]**2 for k in dr] for i in pr]
                        ### compute pooled variance
                        ##### special case 1 :(
                        if m == nproc:
                            pvar = [variance([means[i][k] for i in pr], obmean[k]) for k in dr]
                        else:
                            pvar = [sum([obvar[i][k]*(num_rands[i] - 1) for i in pr])/(m - nproc) for k in dr]
                        ### compute standard error
                        obse = tuple([sqrt(pvar[k]/m) for k in dr])
            self.crn_check(m)
        return isfeas, obmean, obse


class DeterministicOrc(object):
    """Base class for implementing deterministic problems."""

    def hit(self, x, m=1):
        """Generate the deterministic objective values g(x).

        Positional Arguments:
        x -- point to generate estimates
        m -- number of estimates to generate at x. Anything greater than 0 is
            identical for deterministic problems

        Return Values:
        isfeas -- boolean indicating feasibility of x
        omean -- g(x) (tuple of lenth self.num_obj)
        ose -- 0 (tuple of lenth self.num_obj)
        """
        d = self.num_obj
        isfeas = False
        objd = []
        ose = []
        if m > 0:
            isfeas, objd = self.g(x)
            ose = tuple([0 for o in objd])
        return isfeas, objd, ose
