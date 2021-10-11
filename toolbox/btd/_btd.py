# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
""" Eigenchannel calculator for any number of electrodes

Developer: Nick Papior
Contact: nickpapior <at> gmail.com
sisl-version: >=0.11.0
tbtrans-version: >=siesta-4.1.5

This eigenchannel calculater uses TBtrans output to calculate the eigenchannels
for N-terminal systems. In the future this will get transferred to the TBtrans code
but for now this may be used for arbitrary geometries.

It requires two inputs and has several optional flags.

- The siesta.TBT.nc file which contains the geometry that is to be calculated for
  The reason for using the siesta.TBT.nc file is the ease of use:

    The siesta.TBT.nc contains electrode atoms and device atoms. Hence it
    becomes easy to read in the electrode atomic positions.
    Note that since you'll always do a 0 V calculation this isn't making
    any implications for the requirement of the TBT.nc file.
"""
from numbers import Integral
from functools import lru_cache
import os.path as osp

import numpy as np
from numpy import einsum
from numpy import conjugate as conj
#from scipy.linalg import sqrtm
import scipy.sparse as ssp
from scipy.sparse import csr_matrix
from scipy.sparse.linalg import svds

import sisl as si
from sisl import _array as _a
from sisl.linalg import *
from sisl.utils.misc import PropertyDict


arangei = _a.arangei
indices_only = si._indices.indices_only
indices = si._indices.indices

__all__ = ['PivotSelfEnergy', 'DownfoldSelfEnergy', 'DeviceGreen']


def dagger(M):
    return conj(M.T)


def signsqrt(A):
    """ Calculate the sqrt of the elements `A` by retaining the sign.

    This only influences negative values in `A` by returning ``-abs(A)**0.5``
    """
    return np.sign(A) * np.sqrt(np.fabs(A))


def sqrtm(H):
    """ Calculate the sqrt of the Hermitian matrix `H`

    We do this by using eigh and taking the sqrt of the eigenvalues.

    This yields a slightly better value compared to scipy.linalg.sqrtm
    when comparing H12 @ H12 vs. H12 @ H12.T.conj(). The latter is what
    we need.
    """
    e, ev = eigh(H)
    sqe = signsqrt(e)
    return (ev * sqe) @ ev.conj().T


def get_maxerrr(u):
    inner = conj(u.T) @ u
    np.fill_diagonal(inner, inner.diagonal() - 1.)
    a = np.absolute(inner)
    aidx = np.argmax(a)
    uidx = np.argmax(np.absolute(u))
    return a.max(), a[:, 0], np.unravel_index(aidx, a.shape), u.ravel()[uidx], np.unravel_index(uidx, a.shape)


def gram_schmidt(u, modified=True):
    """ Assumes u is in fortran indexing as returned from eigh

    Gram-Schmidt orthogonalization is not always a good idea.

    1. When some of the states die out the precision of the norm
       becomes extremely important and quite often it will blow up.

    2. DOS normalization will be lost if GS is done.

    3. It is not clear whether GS done in each block or at the end
       is the best choice.
    """
    # first normalize
    norm = np.empty(u.shape[1], dtype=si._help.dtype_complex_to_real(u.dtype))

    # we know that u[:, -1] is the largest eigenvector, so we use that
    # as the default
    if modified:
        for i in range(u.shape[1] - 2, -1, -1):
            norm[i+1] = (conj(u[:, i+1]) @ u[:, i+1]).real
            cu = conj(u[:, i])
            for j in range(u.shape[1] - 1, i, -1):
                u[:, i] -= (cu @ u[:, j]) * u[:, j] / norm[j]

    else:
        for i in range(u.shape[1] - 2, -1, -1):
            norm[i+1] = (conj(u[:, i+1]) @ u[:, i+1]).real
            u[:, i] -= (((conj(u[:, i]) @ u[:, i+1:]) / norm[i+1:]).reshape(1, -1) * u[:, i+1:]).sum(1)


def _scat_state_svd(A, **kwargs):
    """ Calculating the SVD of matrix A for the scattering state

    Parameters
    ----------
    A : numpy.ndarray
       matrix to obtain SVD from
    scale : bool or float, optional
       whether to scale matrix `A` to be above ``1e-12`` or by a user-defined number
    lapack_driver : str, optional
       driver queried from `scipy.linalg.svd`
    """
    scale = kwargs.get("scale", False)
    # Scale matrix by a factor to lie in [1e-12; inf[
    if isinstance(scale, bool):
        if scale:
            _ = np.floor(np.log10(np.absolute(A).min())).astype(int)
            if _ < -12:
                scale = 10 ** (-12 - _)
            else:
                scale = False

    # Numerous accounts of SVD algorithms using gesdd results
    # in poor results when min(M, N) >= 26 (block size).
    # This may be an error in the D&C algorithm.
    # Here we resort to precision over time, but user may decide.
    driver = kwargs.get("driver", "gesvd").lower()
    if driver in ('arpack', 'lobpcg', 'sparse'):
        if driver == 'sparse':
            driver = 'arpack' # scipy default

        # filter out keys for scipy.sparse.svds
        svds_kwargs = {key: kwargs[key] for key in ('k', 'ncv', 'tol', 'v0')
                       if key in kwargs}
        # do not calculate vt
        svds_kwargs['return_singular_vectors'] = 'u'
        svds_kwargs['solver'] = driver
        if 'k' not in svds_kwargs:
            k = A.shape[1] // 2
            if k < 3:
                k = A.shape[1] - 1
            svds_kwargs['k'] = k

        if scale:
            A, DOS, _ = svds(A * scale, **svds_kwargs)
            DOS /= scale
        else:
            A, DOS, _ = svds(A, **svds_kwargs)

    else:
        # it must be a lapack driver:
        if scale:
            A, DOS, _ = svd_destroy(A * scale, full_matrices=False, check_finite=False,
                                    lapack_driver=driver)
            DOS /= scale
        else:
            A, DOS, _ = svd_destroy(A, full_matrices=False, check_finite=False,
                                    lapack_driver=driver)
        del _

    return DOS ** 2 / (2 * np.pi), A


class PivotSelfEnergy(si.physics.SelfEnergy):
    """ Container for the self-energy object

    This may either be a `tbtsencSileTBtrans`, a `tbtgfSileTBtrans` or a sisl.SelfEnergy objectfile
    """

    def __init__(self, name, se, pivot=None):
        # Name of electrode
        self.name = name

        # File containing the self-energy
        # This may be either of:
        #  tbtsencSileTBtrans
        #  tbtgfSileTBtrans
        #  SelfEnergy object (for direct calculation)
        self._se = se

        if isinstance(se, si.io.tbtrans.tbtsencSileTBtrans):
            def se_func(*args, **kwargs):
                return self._se.self_energy(self.name, *args, **kwargs)
            def scat_func(*args, **kwargs):
                return self._se.scattering_matrix(self.name, *args, **kwargs)
        else:
            def se_func(*args, **kwargs):
                return self._se.self_energy(*args, **kwargs)
            def scat_func(*args, **kwargs):
                return self._se.scattering_matrix(*args, **kwargs)

        # Store the pivoting for faster indexing
        if pivot is None:
            if not isinstance(se, si.io.tbtrans.tbtsencSileTBtrans):
                raise ValueError(f"{self.__class__.__name__} must be passed a sisl.io.tbtrans.tbtsencSileTBtrans. "
                                 "Otherwise use the DownfoldSelfEnergy method with appropriate arguments.")
            pivot = se

        # Pivoting indices for the self-energy for the device region
        # but with respect to the full system size
        self.pvt = pivot.pivot(name).reshape(-1, 1)

        # Pivoting indices for the self-energy for the device region
        # but with respect to the device region only
        self.pvt_dev = pivot.pivot(name, in_device=True).reshape(-1, 1)

        # the pivoting in the downfolding region (with respect to the full
        # system size)
        self.pvt_down = pivot.pivot_down(name).reshape(-1, 1)

        # Retrieve BTD matrices for the corresponding electrode
        self.btd = pivot.btd(name)

        # Get the individual matrices
        cbtd = np.cumsum(self.btd)
        pvt_btd = []
        o = 0
        for i in cbtd:
            # collect the pivoting indices for the downfolding
            pvt_btd.append(self.pvt_down[o:i, 0])
            o += i
        #self.pvt_btd = np.concatenate(pvt_btd).reshape(-1, 1)
        #self.pvt_btd_sort = arangei(o)

        self._se_func = se_func
        self._scat_func = scat_func

    def __len__(self):
        return len(self.pvt_dev)

    def self_energy(self, *args, **kwargs):
        return self._se_func(*args, **kwargs)

    def scattering_matrix(self, *args, **kwargs):
        return self._scat_func(*args, **kwargs)


class DownfoldSelfEnergy(PivotSelfEnergy):

    def __init__(self, name, se, pivot, Hdevice, bulk=True, bloch=(1, 1, 1)):
        super().__init__(name, se, pivot)

        if np.allclose(bloch, 1):
            def _bloch(func, k, E, *args, **kwargs):
                return func(E, k, *args, **kwargs)
            self._bloch = _bloch
        else:
            self._bloch = si.Bloch(bloch)

        # To re-create the downfoldable self-energies we need a few things:
        # pivot == for pivoting indices and BTD downfolding region
        # se == SelfEnergy for calculating self-energies and scattering matrix
        # Hdevice == device H for downfolding the electrode self-energy
        # bulk == whether the electrode self-energy argument should be passed bulk
        #         or not
        # name == just the name

        # storage data
        self._data = PropertyDict()
        self._data.bulk = bulk

        # Retain the device for only the downfold region
        # a_down is sorted!
        a_elec = pivot.a_elec(self.name)

        # Now figure out all the atoms in the downfolding region
        # pivot_down is the electrode + all orbitals including the orbitals
        # reaching into the device
        pivot_down = pivot.pivot_down(self.name)
        # note that the last orbitals in pivot_down is the returned self-energies
        # that we want to calculate in this class

        geometry = pivot.geometry
        # Figure out the full device part of the downfolding region
        down_atoms = geometry.o2a(pivot_down, unique=True).astype(np.int32, copy=False)
        down_orbitals = geometry.a2o(down_atoms, all=True).astype(np.int32, copy=False)

        # The orbital indices in self.H.device.geometry
        # which transfers the orbitals to the downfolding region

        # Now we need to figure out the pivoting indices from the sub-set
        # geometry

        self._data.H = PropertyDict()
        self._data.H.electrode = se.spgeom0
        self._data.H.device = Hdevice.sub(down_atoms)
        geometry_down = self._data.H.device.geometry

        # Now we retain the positions of the electrode orbitals in the
        # non pivoted structure for inserting the self-energy
        # Once the down-folded matrix is formed we can pivot it
        # in the BTD class
        pvt = indices(down_atoms, a_elec)
        self._data.elec = geometry_down.a2o(pvt[pvt >= 0], all=True).reshape(-1, 1)
        pvt = indices(down_orbitals, pivot_down)
        self._data.dev = pvt[pvt >= 0].reshape(-1, 1)

        # Create BTD indices
        self._data.cumbtd = np.append(0, np.cumsum(self.btd))

    def __len__(self):
        return len(self._data.dev)

    def _prepare(self, E, k=(0, 0, 0)):
        if hasattr(self._data, "E"):
            if np.allclose(self._data.E, E) and np.allclose(self._data.k, k):
                # we have already prepared the calculation
                return

        # Prepare the matrices
        data = self._data
        H = data.H

        data.SeH = H.device.Sk(k, dtype=np.complex128) * E - H.device.Hk(k, dtype=np.complex128)
        if self.bulk:
            E_bulk = E
            if np.isrealobj(E):
                try:
                    E_bulk = E + 1j * self._se.eta
                except:
                    pass
            data.SeH[data.elec, data.elec.T] = H.electrode.Sk(k, dtype=np.complex128) * E_bulk - H.electrode.Hk(k, dtype=np.complex128)
        data.E = E
        data.k = _a.asarrayd(k)

    def self_energy(self, E, k=(0, 0, 0), *args, **kwargs):
        self._prepare(E, k)
        data = self._data
        se = self._bloch(super().self_energy, k, *args, E=E, **kwargs)

        # now put it in the matrix
        M = data.SeH.copy()
        M[data.elec, data.elec.T] -= se

        # transfer to BTD
        pvt = data.dev
        cumbtd = data.cumbtd

        def gM(M, idx1, idx2):
            return M[idx1, idx2].toarray()

        Mr = 0
        pvt_i = pvt[cumbtd[0]:cumbtd[1]].reshape(-1, 1)
        for b in range(len(self.btd) - 1):
            pvt_i1 = pvt[cumbtd[b+1]:cumbtd[b+2]].reshape(-1, 1)

            Mr = gM(M, pvt_i1, pvt_i.T) @ solve(gM(M, pvt_i, pvt_i.T) - Mr,
                                                gM(M, pvt_i, pvt_i1.T),
                                                overwrite_a=True, overwrite_b=True)
            pvt_i = pvt_i1

        return Mr

    def scattering_matrix(self, *args, **kwargs):
        return self.se2scat(self.self_energy(*args, **kwargs))


class BlockMatrixIndexer:
    def __init__(self, bm):
        self._bm = bm

    def __len__(self):
        return len(self._bm.blocks)

    def __getitem__(self, key):
        if not isinstance(key, tuple):
            raise ValueError(f"{self.__class__.__name__} index retrieval must be done with a tuple.")
        M = self._bm._M.get(key)
        if M is None:
            i, j = key
            # the data-type is probably incorrect.. :(
            return np.zeros([self._bm.blocks[i], self._bm.blocks[j]])
        return M

    def __setitem__(self, key, M):
        if not isinstance(key, tuple):
            raise ValueError(f"{self.__class__.__name__} index setting must be done with a tuple.")
        self._bm._M[key] = M


class BlockMatrix:
    """ Container class that holds a block matrix """

    def __init__(self, blocks):
        self._blocks = blocks
        self._M = {}

    @property
    def blocks(self):
        return self._blocks

    def toarray(self):
        BI = self.block_indexer
        nb = len(BI)
        # stack stuff together
        return np.concatenate([
            np.concatenate([BI[i, j] for i in range(nb)], axis=0)
            for j in range(nb)], axis=1)

    def tobtd(self):
        """ Return only the block tridiagonal part of the matrix """
        ret = self.__class__(self.blocks)
        sBI = self.block_indexer
        rBI = ret.block_indexer
        nb = len(sBI)
        nbm1 = nb - 1
        for j in range(nb):
            for i in range(max(0, j-1), min(j+1, nbm1)):
                rBI[i, j] = sBI[i, j]
        return ret

    def diagonal(self):
        BI = self.block_indexer
        return np.concatenate([BI[b, b].diagonal() for b in range(len(BI))])

    @property
    def block_indexer(self):
        return BlockMatrixIndexer(self)


class DeviceGreen:
    r""" Block-tri-diagonal Green function calculator

    This class enables the extraction and calculation of some important
    quantities not currently accessible in TBtrans.

    For instance it may be used to calculate scattering states from
    the Green function.
    Once scattering states have been calculated one may also calculate
    the eigenchannels.

    Both calculations are very efficient and uses very little memory
    compared to the full matrices normally used.

    Consider a regular 2 electrode setup with transport direction
    along the 3rd lattice vector. Then the following example may
    be used to calculate the eigen-channels:

    .. code::

       import sisl
       from sisl_toolbox.btd import *
       # First read in the required data
       H_elec = sisl.Hamiltonian.read("ELECTRODE.nc")
       H = sisl.Hamiltonian.read("DEVICE.nc")
       # remove couplings along the self-energy direction
       # to ensure no fake couplings.
       H.set_nsc(c=1)

       # Read in a single tbtrans output which contains the BTD matrices
       # and instructs this class how it should pivot the matrix to obtain
       # a BTD matrix.
       tbt = sisl.get_sile("siesta.TBT.nc")

       # Define the self-energy calculators which will downfold the
       # self-energies into the device region.
       # Since a downfolding will be done it requires the device Hamiltonian.
       H_elec.shift(tbt.mu("Left"))
       left = DownfoldSelfEnergy("Left", s.RecursiveSI(H_elec, '-C', eta=tbt.eta("Left"),
                                 tbt, H)
       H_elec.shift(tbt.mu("Right") - tbt.mu("Left"))
       left = DownfoldSelfEnergy("Right", s.RecursiveSI(H_elec, '+C', eta=tbt.eta("Right"),
                                 tbt, H)

       G = DeviceGreen(H, [left, right], tbt)

       # Calculate the scattering state from the left electrode
       # and then the eigen channels to the right electrode
       state = G.scattering_state("Left", E=0.1)
       eig_channel = G.eigenchannel(state, "Right")

    To make this easier there exists a short-hand version that does the
    above:

    .. code::

       G = DeviceGreen.from_fdf("RUN.fdf")

    which reads all variables from the FDF file and parses them accordingly.
    This does not take all things into consideration, but should cover most problems.
    """

    # TODO we should speed this up by overwriting A with the inverse once
    #      calculated. We don't need it at that point.
    #      That would probably require us to use a method to retrieve
    #      the elements which determines if it has been calculated or not.

    def __init__(self, H, elecs, pivot):
        """ Create Green function with Hamiltonian and BTD matrix elements """
        self.H = H

        # Store electrodes (for easy retrieval of the SE)
        # There may be no electrodes
        self.elecs = elecs
        #self.elecs_pvt = [pivot.pivot(el.name).reshape(-1, 1)
        #                  for el in elecs]
        self.elecs_pvt_dev = [pivot.pivot(el.name, in_device=True).reshape(-1, 1)
                              for el in elecs]

        self.pvt = pivot.pivot()
        self.btd = pivot.btd()

        # Create BTD indices
        self.btd_cum = np.cumsum(self.btd)
        #cumbtd = np.append(0, self.btd_cum)

        #self.btd_idx = [self.pvt[cumbtd[i]:cumbtd[i+1]]
        #                for i in range(len(self.btd))]
        self.reset()

    @classmethod
    def from_fdf(cls, fdf, prefix='TBT'):
        """ Return a new `DeviceGreen` using information gathered from the fdf

        Parameters
        ----------
        fdf : str or fdfSileSiesta
           fdf file to read the parameters from
        prefix : {'TBT', 'TS'}
           which prefix to use, if TBT it will prefer TBT prefix, but fall back
           to TS prefixes.
           If TS, only those prefixes will be used.
        """
        if not isinstance(fdf, si.BaseSile):
            fdf = si.io.siesta.fdfSileSiesta(fdf)

        # Now read the values needed
        slabel = fdf.get("SystemLabel", "siesta")
        # Test if the TBT output file exists:
        tbt = None
        for end in ["TBT.nc", "TBT_UP.nc", "TBT_DN.nc"]:
            if osp.exists(f"{slabel}.{end}"):
                tbt = f"{slabel}.{end}"
        if tbt is None:
            raise FileNotFoundError(f"{cls.__name__}.from_fdf could "
                                    f"not find file {slabel}.[TBT|TBT_UP|TBT_DN].nc")
        tbt = si.get_sile(tbt)

        # Read the device H, only valid for TBT stuff
        Hdev = si.get_sile(fdf.get("TBT.HS", f"{slabel}.TSHS")).read_hamiltonian()

        def get_line(line):
            """ Parse lines in the %block constructs of fdf's """
            key, val = line.split(" ", 1)
            return key.lower().strip(), val.split('#', 1)[0].strip()

        def read_electrode(elec_prefix):
            """ Parse the electrode information and return a dictionary with content """
            from sisl.unit.siesta import unit_convert
            ret = PropertyDict()

            is_tbtrans = prefix.upper() == "TBT"
            if is_tbtrans:
                def block_get(dic, key, default=None, unit=None):
                    ret = dic.get(f"tbt.{key}", dic.get(key, default))
                    if unit is None or not isinstance(ret, str):
                        return ret
                    ret, un = ret.split()
                    return float(ret) * unit_convert(un, unit)
            else:
                def block_get(dic, key, default=None, unit=None):
                    ret = dic.get(key, default)
                    if unit is None or not isinstance(ret, str):
                        return ret
                    ret, un = ret.split()
                    return float(ret) * unit_convert(un, unit)

            tbt_prefix = f"TBT.{elec_prefix}"
            ts_prefix = f"TS.{elec_prefix}"

            block = fdf.get(f"{ts_prefix}")
            Helec = fdf.get(f"{ts_prefix}.HS")
            eta = fdf.get(f"TS.Elecs.Eta", 1e-4, unit='eV')
            bloch = [1, 1, 1]
            for i in range(3):
                bloch[i] = fdf.get(f"{ts_prefix}.Bloch.A{i+1}", 1)
            if is_tbtrans:
                block = fdf.get(f"{tbt_prefix}", block)
                Helec = fdf.get(f"{tbt_prefix}.HS", Helec)
                eta = fdf.get(f"TBT.Elecs.Eta", eta, unit='eV')
                for i in range(3):
                    bloch[i] = fdf.get(f"{tbt_prefix}.Bloch.A{i+1}", bloch[i])

            # Convert to key value based function
            dic = {key: val for key, val in map(get_line, block)}

            # Retrieve data
            for key in ("hs", "tshs"):
                Helec = block_get(dic, key, Helec)
            if Helec:
                Helec = si.get_sile(Helec).read_hamiltonian()
            else:
                raise ValueError(f"{self.__class__.__name__}.from_fdf could not find "
                                 f"electrode HS in block: {prefix} ??")

            # Get semi-infinite direction
            semi_inf = None
            for suf in ["-direction", "-dir", ""]:
                semi_inf = block_get(dic, f"semi-inf{suf}", semi_inf)
            if semi_inf is None:
                raise ValueError(f"{self.__class__.__name__}.from_fdf could not find "
                                 f"electrode semi-inf-direction in block: {prefix} ??")
            # convert to sisl infinite
            semi_inf = semi_inf.lower()
            semi_inf = semi_inf[0] + {'a1': 'a', 'a2': 'b', 'a3': 'c'}.get(semi_inf[1:], semi_inf[1:])
            # Check that semi_inf is a recursive one!
            if not semi_inf in ['-a', '+a', '-b', '+b', '-c', '+c']:
                raise NotImplementedError(f"{self.__class__.__name__} does not implement other "
                                          "self energies than the recursive one.")

            bulk = bool(block_get(dic, "bulk", True))
            # loop for 0
            for i, sufs in enumerate([("a", "a1"), ("b", "a2"), ("c", "a3")]):
                for suf in sufs:
                    bloch[i] = block_get(dic, f"bloch-{suf}", bloch[i])

            bloch = [int(b) for b in block_get(dic, "bloch", f"{bloch[0]} {bloch[1]} {bloch[2]}").split()]

            ret.eta = block_get(dic, "eta", eta, unit='eV')
            ret.Helec = Helec
            ret.bloch = bloch
            ret.semi_inf = semi_inf
            ret.bulk = bulk
            return ret

        # Loop electrodes and read in and construct data
        elecs = []
        for elec in tbt.elecs:
            mu = tbt.mu(elec)
            eta = tbt.eta(elec)

            data = read_electrode(f"Elec.{elec}")

            # shift according to potential
            data.Helec.shift(mu)
            se = si.RecursiveSI(data.Helec, data.semi_inf, eta=eta)
            # Limit connections of the device along the semi-inf directions
            # TODO check whether there are systems where it is important
            # we do all set_nsc before passing it for each electrode.
            nsc = Hdev.nsc.copy()
            nsc[se.semi_inf] = 1
            Hdev.set_nsc(nsc)

            elecs.append(DownfoldSelfEnergy(elec, se, tbt, Hdev,
                                            bulk=data.bulk, bloch=data.bloch))

        return cls(Hdev, elecs, tbt)

    def reset(self):
        """ Clean any memory used by this object """
        self._data = PropertyDict()

    def __len__(self):
        return len(self.pvt)

    def _elec(self, elec):
        """ Convert a string electrode to the proper linear index """
        if isinstance(elec, str):
            for iel, el in enumerate(self.elecs):
                if el.name == elec:
                    return iel
        elif isinstance(elec, PivotSelfEnergy):
            return self._elec(elec.name)
        return elec

    def _elec_name(self, elec):
        """ Convert an electrode index or str to the name of the electrode """
        if isinstance(elec, str):
            return elec
        elif isinstance(elec, PivotSelfEnergy):
            return elec.name
        return self.elecs[elec].name

    def _check_Ek(self, E, k):
        if hasattr(self._data, "E"):
            if np.allclose(self._data.E, E) and np.allclose(self._data.k, k):
                # we have already prepared the calculation
                return True
        # while resetting is not necessary, it can
        # save a lot of memory since some arrays are not
        # temporarily stored twice.
        self.reset()
        return False

    def _prepare_se(self, E, k=(0, 0, 0)):
        if self._check_Ek(E, k):
            return

        # Create all self-energies (and store the Gamma's)
        gamma = []
        for elec in self.elecs:
            # Insert values
            SE = elec.self_energy(E, k)
            gamma.append(elec.se2scat(SE))
        self._data.gamma = gamma

    def _prepare(self, E, k=(0, 0, 0)):
        if self._check_Ek(E, k):
            return

        # Prepare the Green function calculation
        data = self._data
        inv_G = self.H.Sk(k, dtype=np.complex128) * E - self.H.Hk(k, dtype=np.complex128)

        # Now reduce the sparse matrix to the device region (plus do the pivoting)
        inv_G = inv_G[self.pvt, :][:, self.pvt]

        # Create all self-energies (and store the Gamma's)
        gamma = []
        for elec in self.elecs:
            # Insert values
            SE = elec.self_energy(E, k)
            inv_G[elec.pvt_dev, elec.pvt_dev.T] -= SE
            gamma.append(elec.se2scat(SE))
        del SE
        data.gamma = gamma

        nb = len(self.btd)

        # Now we have all needed to calculate the inverse parts of the Green function
        A = [None] * nb
        B = [1] * nb
        C = [1] * nb

        # Now we can calculate everything
        cbtd = self.btd_cum
        btd = self.btd

        sl0 = slice(0, cbtd[0])
        slp = slice(cbtd[0], cbtd[1])
        # initial matrix A and C
        iG = inv_G[sl0, :].tocsc()
        A[0] = iG[:, sl0].toarray()
        C[1] = iG[:, slp].toarray()
        for b, bs in enumerate(btd[1:-1], 1):
            # rotate slices
            sln = sl0
            sl0 = slp
            slp = slice(cbtd[b], cbtd[b+1])
            iG = inv_G[sl0, :].tocsc()

            B[b-1] = iG[:, sln].toarray()
            A[b] = iG[:, sl0].toarray()
            C[b+1] = iG[:, slp].toarray()
        # and final matrix A and B
        iG = inv_G[slp, :].tocsc()
        A[-1] = iG[:, slp].toarray()
        B[-2] = iG[:, sl0].toarray()

        # clean-up, not used anymore
        del inv_G

        data.A = A
        data.B = B
        data.C = C

        # Now do propagation forward, tilde matrices
        tX = [0] * nb
        tY = [0] * nb
        # \tilde Y
        tY[1] = solve(A[0], C[1])
        # \tilde X
        tX[-2] = solve(A[-1], B[-2])
        for n in range(2, nb):
            p = nb - n - 1
            # \tilde Y
            tY[n] = solve(A[n-1] - B[n-2] @ tY[n-1], C[n], overwrite_a=True)
            # \tilde X
            tX[p] = solve(A[p+1] - C[p+2] @ tX[p+1], B[p], overwrite_a=True)

        data.tX = tX
        data.tY = tY
        data.E = E
        data.k = _a.asarrayd(k)

    def green(self, E, k=(0, 0, 0), format='array'):
        r""" Calculate the Green function for a given `E` and `k` point

        The Green function is calculated as:

        .. math::
            \mathbf G(E,\mathbf k) = \big[\mathbf S(\mathbf k) E - \mathbf H(\mathbf k)
                  - \sum \boldsymbol \Sigma(E,\mathbf k)\big]^{-1}

        Parameters
        ----------
        E : float
           the energy to calculate at, may be a complex value.
        k : array_like, optional
           k-point to calculate the Green function at
        """
        self._prepare(E, k)
        format = format.lower()
        if format in ('array', 'dense'):
            return self._green_array()
        elif format in ('sparse',):
            return self._green_sparse()
        elif format in ('btd',):
            return self._green_btd()
        elif format in ('bm',):
            return self._green_bm()
        raise ValueError(f"{self.__class__.__name__}.green 'format' not valid input [array,sparse,btd/bm]")

    def _green_array(self):
        n = len(self.pvt)
        G = np.empty([n, n], dtype=self._data.A[0].dtype)

        btd = self.btd
        nb = len(btd)
        nbm1 = nb - 1
        sumbs = 0
        A = self._data.A
        B = self._data.B
        C = self._data.C
        tX = self._data.tX
        tY = self._data.tY
        for b, bs in enumerate(btd):
            bsn = btd[b - 1]
            if b < nbm1:
                bsp = btd[b + 1]

            sl0 = slice(sumbs, sumbs + bs)

            # Calculate diagonal part
            if b == 0:
                G[sl0, sl0] = inv_destroy(A[b] - C[b + 1] @ tX[b])
            elif b == nbm1:
                G[sl0, sl0] = inv_destroy(A[b] - B[b - 1] @ tY[b])
            else:
                G[sl0, sl0] = inv_destroy(A[b] - B[b - 1] @ tY[b] - C[b + 1] @ tX[b])

            # Do above
            next_sum = sumbs
            slp = sl0
            for a in range(b - 1, -1, -1):
                # Calculate all parts above
                sla = slice(next_sum - btd[a], next_sum)
                G[sla, sl0] = - tY[a + 1] @ G[slp, sl0]
                slp = sla
                next_sum -= btd[a]

            sl0 = slice(sumbs, sumbs + bs)

            # Step block
            sumbs += bs

            # Do below
            next_sum = sumbs
            slp = sl0
            for a in range(b + 1, nb):
                # Calculate all parts above
                sla = slice(next_sum, next_sum + btd[a])
                G[sla, sl0] = - tX[a - 1] @ G[slp, sl0]
                slp = sla
                next_sum += btd[a]

        return G

    def _green_btd(self):
        G = BlockMatrix(self.btd)
        BI = G.block_indexer
        nb = len(BI)
        nbm1 = nb - 1
        A = self._data.A
        B = self._data.B
        C = self._data.C
        tX = self._data.tX
        tY = self._data.tY
        for b in range(nb):
            # Calculate diagonal part
            if b == 0:
                G11 = inv_destroy(A[b] - C[b + 1] @ tX[b])
            elif b == nbm1:
                G11 = inv_destroy(A[b] - B[b - 1] @ tY[b])
            else:
                G11 = inv_destroy(A[b] - B[b - 1] @ tY[b] - C[b + 1] @ tX[b])

            BI[b, b] = G11
            # do above
            if b > 0:
                BI[b - 1, b] = - tY[b] @ G11
            # do below
            if b < nbm1:
                BI[b + 1, b] = - tX[b] @ G11

        return G

    def _green_bm(self):
        G = self._green_btd()
        BI = G.block_indexer
        nb = len(BI)
        nbm1 = nb - 1

        tX = self._data.tX
        tY = self._data.tY
        for b in range(nb):
            G0 = BI[b, b]
            for bb in range(b, 0, -1):
                G0 = - tY[bb] @ G0
                BI[bb-1, b] = G0
            G0 = BI[b, b]
            for bb in range(b, nbm1):
                G0 = - tX[bb] @ G0
                BI[bb+1, b] = G0

        return G

    def _green_sparse(self):
        n = len(self.pvt)

        # create a sparse matrix
        G = self.H.Sk(format='csr', dtype=self._data.A[0].dtype)
        # pivot the matrix
        G = G[self.pvt, :][:, self.pvt]

        # Get row and column entries
        ncol = np.diff(G.indptr)
        row = (ncol > 0).nonzero()[0]
        # Now we have [0 0 0 0 1 1 1 1 2 2 ... no-1 no-1]
        row = np.repeat(row.astype(np.int32, copy=False), ncol[row])
        col = G.indices

        def get_idx(row, col, row_b, col_b=None):
            if col_b is None:
                col_b = row_b
            idx = (row_b[0] <= row).nonzero()[0]
            idx = idx[row[idx] < row_b[1]]
            idx = idx[col_b[0] <= col[idx]]
            return idx[col[idx] < col_b[1]]

        btd = self.btd
        nb = len(btd)
        nbm1 = nb - 1
        A = self._data.A
        B = self._data.B
        C = self._data.C
        tX = self._data.tX
        tY = self._data.tY
        sumbsn, sumbs, sumbsp = 0, 0, 0
        for b, bs in enumerate(btd):
            sumbsp = sumbs + bs
            if b < nbm1:
                bsp = btd[b + 1]

            # Calculate diagonal part
            if b == 0:
                GM = inv_destroy(A[b] - C[b + 1] @ tX[b])
            elif b == nbm1:
                GM = inv_destroy(A[b] - B[b - 1] @ tY[b])
            else:
                GM = inv_destroy(A[b] - B[b - 1] @ tY[b] - C[b + 1] @ tX[b])

            # get all entries where G is non-zero
            idx = get_idx(row, col, (sumbs, sumbsp))
            G.data[idx] = GM[row[idx] - sumbs, col[idx] - sumbs]

            # check if we should do block above
            if b > 0:
                idx = get_idx(row, col, (sumbsn, sumbs), (sumbs, sumbsp))
                if len(idx) > 0:
                    G.data[idx] = -(tY[b] @ GM)[row[idx] - sumbsn, col[idx] - sumbs]

            # check if we should do block below
            if b < nbm1:
                idx = get_idx(row, col, (sumbsp, sumbsp + bsp), (sumbs, sumbsp))
                if len(idx) > 0:
                    G.data[idx] = -(tX[b] @ GM)[row[idx] - sumbsp, col[idx] - sumbs]

            bsn = bs
            sumbsn = sumbs
            sumbs += bs

        return G

    def _green_diag_block(self, idx):
        nb = len(self.btd)
        nbm1 = nb - 1

        # Find parts we need to calculate
        block1 = (idx.min() < self.btd_cum).nonzero()[0][0]
        block2 = (idx.max() < self.btd_cum).nonzero()[0][0]
        if block1 == block2:
            blocks = [block1]
        else:
            blocks = list(range(block1, block2+1))
        assert len(blocks) <= 2, f"{self.__class__.__name__} requires G calculation for only 1 or 2 blocks"

        n = self.btd[blocks].sum()
        G = np.empty([n, len(idx)], dtype=self._data.A[0].dtype)

        btd = self.btd
        c = np.append(0, self.btd_cum)
        A = self._data.A
        B = self._data.B
        C = self._data.C
        tX = self._data.tX
        tY = self._data.tY
        for b in blocks:
            # Find the indices in the block
            i = idx[c[b] <= idx].copy()
            i = i[i < c[b + 1]].astype(np.int32)

            c_idx = arangei(c[b], c[b + 1]).reshape(-1, 1)
            b_idx = indices_only(c_idx.ravel(), i)
            # Subtract the first block to put it only in the sub-part
            c_idx -= c[blocks[0]]

            if b == blocks[0]:
                sl = slice(0, btd[b])
                r_idx = arangei(len(b_idx))
            else:
                sl = slice(btd[blocks[0]], btd[blocks[0]] + btd[b])
                r_idx = arangei(len(idx) - len(b_idx), len(idx))

            if b == 0:
                G[sl, r_idx] = inv_destroy(A[b] - C[b + 1] @ tX[b])[:, b_idx]
            elif b == nbm1:
                G[sl, r_idx] = inv_destroy(A[b] - B[b - 1] @ tY[b])[:, b_idx]
            else:
                G[sl, r_idx] = inv_destroy(A[b] - B[b - 1] @ tY[b] - C[b + 1] @ tX[b])[:, b_idx]

            if len(blocks) == 1:
                break

            # Now calculate the thing (below/above)
            if b == blocks[0]:
                # Calculate below
                slp = slice(btd[b], btd[b] + btd[blocks[1]])
                G[slp, r_idx] = - tX[b] @ G[sl, r_idx]
            else:
                # Calculate above
                slp = slice(0, btd[blocks[0]])
                G[slp, r_idx] = - tY[b] @ G[sl, r_idx]

        return blocks, G

    def _green_column(self, idx):
        # To calculate the full Gf for specific column indices
        # These indices should maximally be spanning 2 blocks
        nb = len(self.btd)
        nbm1 = nb - 1

        # Find parts we need to calculate
        block1 = (idx.min() < self.btd_cum).nonzero()[0][0]
        block2 = (idx.max() < self.btd_cum).nonzero()[0][0]
        if block1 == block2:
            blocks = [block1]
        else:
            blocks = [block1, block2]
            assert block1 + 1 == block2, (f"{self.__class__.__name__} Green column requires "
                                          "consecutive indices maximally spanning 2 blocks.")
        # We can only have 2 consecutive blocks for
        # a Gamma, so same for BTD
        assert len(blocks) <= 2, (f"{self.__class__.__name__} Green column requires "
                                  "consecutive indices maximally spanning 2 blocks.")

        n = len(self)
        G = np.empty([n, len(idx)], dtype=self._data.A[0].dtype)

        c = np.append(0, self.btd_cum)
        A = self._data.A
        B = self._data.B
        C = self._data.C
        tX = self._data.tX
        tY = self._data.tY
        for b in blocks:
            # Find the indices in the block
            i = idx[c[b] <= idx]
            i = i[i < c[b + 1]].astype(np.int32)

            c_idx = arangei(c[b], c[b + 1]).reshape(-1, 1)
            b_idx = indices_only(c_idx.ravel(), i)

            if b == blocks[0]:
                r_idx = arangei(len(b_idx))
            else:
                r_idx = arangei(len(idx) - len(b_idx), len(idx))

            sl = slice(c[b], c[b + 1])
            if b == 0:
                G[sl, r_idx] = inv_destroy(A[b] - C[b + 1] @ tX[b])[:, b_idx]
            elif b == nbm1:
                G[sl, r_idx] = inv_destroy(A[b] - B[b - 1] @ tY[b])[:, b_idx]
            else:
                G[sl, r_idx] = inv_destroy(A[b] - B[b - 1] @ tY[b] - C[b + 1] @ tX[b])[:, b_idx]

            if len(blocks) == 1:
                break

            # Now calculate the thing (above below)
            sl = slice(c[b], c[b + 1])
            if b == blocks[0] and b < nb - 1:
                # Calculate below
                slp = slice(c[b + 1], c[b + 2])
                G[slp, r_idx] = - tX[b] @ G[sl, r_idx]
            elif b > 0:
                # Calculate above
                slp = slice(c[b - 1], c[b])
                G[slp, r_idx] = - tY[b] @ G[sl, r_idx]

        # Now we can calculate the Gf column above
        b = blocks[0]
        slp = slice(c[b], c[b + 1])
        for b in range(blocks[0] - 1, -1, -1):
            sl = slice(c[b], c[b + 1])
            G[sl, :] = - tY[b + 1] @ G[slp, :]
            slp = sl

        # All blocks below
        b = blocks[-1]
        slp = slice(c[b], c[b + 1])
        for b in range(blocks[-1] + 1, nb):
            sl = slice(c[b], c[b + 1])
            G[sl, :] = - tX[b - 1] @ G[slp, :]
            slp = sl

        return G

    def spectral(self, elec, E, k=(0, 0, 0), format='array', method='column', herm=True):
        r""" Calculate the spectral function for a given `E` and `k` point from a given electrode

        The spectral function is calculated as:

        .. math::
            \mathbf A_{\mathfrak{e}}(E,\mathbf k) = \mathbf G(E,\mathbf k)\boldsymbol\Gamma_{\mathfrak{e}}(E,\mathbf k)
                   \mathbf G^\dagger(E,\mathbf k)

        Parameters
        ----------
        elec : str or int
           the electrode to calculate the spectral function from
        E : float
           the energy to calculate at, may be a complex value.
        k : array_like, optional
           k-point to calculate the spectral function at
        method : {'column', 'propagate'}
           which method to use for calculating the spectral function.
           Depending on the size of the BTD blocks one may be faster than the
           other. For large systems you are recommended to time the different methods
           and stick with the fastest one, they are numerically identical.
        """
        # the herm flag is considered useful for testing, there is no need to
        # play with it. So it isn't documented.

        elec = self._elec(elec)
        self._prepare(E, k)
        format = format.lower()
        method = method.lower()
        if format in ('array', 'dense'):
            if method == 'column':
                return self._spectral_column(elec, herm)
            elif method == 'propagate':
                return self._spectral_propagate(elec, herm)
        elif format in ('btd',):
            if method == 'column':
                return self._spectral_column_btd(elec, herm)
        elif format in ('bm',):
            if method == 'column':
                return self._spectral_column_bm(elec, herm)
        raise ValueError(f"{self.__class__.__name__}.spectral combination of format+method not recognized {format}+{method}.")

    def _spectral_column(self, elec, herm):
        G = self._green_column(self.elecs_pvt_dev[elec].ravel())
        # Now calculate the full spectral function
        return G @ self._data.gamma[elec] @ dagger(G)

    def _spectral_column_btd(self, elec, herm):
        G = self._green_column(self.elecs_pvt_dev[elec].ravel())
        nb = len(self.btd)
        nbm1 = nb - 1

        Gam = self._data.gamma[elec]

        # Now calculate the full spectral function
        btd = BlockMatrix(self.btd)
        BI = btd.block_indexer

        c = np.append(0, self.btd_cum)
        if herm:
            # loop columns
            for jb in range(nb):
                slj = slice(c[jb], c[jb+1])
                Gj = Gam @ dagger(G[slj, :])
                for ib in range(max(0, jb - 1), jb):
                    sli = slice(c[ib], c[ib+1])
                    BI[ib, jb] = G[sli, :] @ Gj
                    BI[jb, ib] = BI[ib, jb].T.conj()
                BI[jb, jb] = G[slj, :] @ Gj

        else:
            # loop columns
            for jb in range(nb):
                slj = slice(c[jb], c[jb+1])
                Gj = Gam @ dagger(G[slj, :])
                for ib in range(max(0, jb-1), min(jb+1, nbm1)):
                    sli = slice(c[ib], c[ib+1])
                    BI[ib, jb] = G[sli, :] @ Gj

        return btd

    def _spectral_column_bm(self, elec, herm):
        G = self._green_column(self.elecs_pvt_dev[elec].ravel())
        nb = len(self.btd)
        nbm1 = nb - 1

        Gam = self._data.gamma[elec]

        # Now calculate the full spectral function
        btd = BlockMatrix(self.btd)
        BI = btd.block_indexer

        c = np.append(0, self.btd_cum)

        if herm:
            # loop columns
            for jb in range(nb):
                slj = slice(c[jb], c[jb+1])
                Gj = Gam @ dagger(G[slj, :])
                for ib in range(jb):
                    sli = slice(c[ib], c[ib+1])
                    BI[ib, jb] = G[sli, :] @ Gj
                    BI[jb, ib] = BI[ib, jb].T.conj()
                BI[jb, jb] = G[slj, :] @ Gj

        else:
            # loop columns
            for jb in range(nb):
                slj = slice(c[jb], c[jb+1])
                Gj = Gam @ dagger(G[slj, :])
                for ib in range(nb):
                    sli = slice(c[ib], c[ib+1])
                    BI[ib, jb] = G[sli, :] @ Gj

        return btd

    def _spectral_propagate_btd(self, elec, herm):
        raise NotImplementedError
        nb = len(self.btd)
        nbm1 = nb - 1

        btd = BlockMatrix(self.btd)
        BI = btd.block_indexer

        # First we need to calculate diagonal blocks of the spectral matrix
        blocks, A = self._green_diag_block(self.elecs_pvt_dev[elec].ravel())
        A = A @ self._data.gamma[elec] @ dagger(A)

        c = np.append(0, self.btd_cum)
        BI[blocks[0], blocks[0]] = A

        # now loop backwards
        tX = self._data.tX
        tY = self._data.tY

    def _spectral_propagate(self, elec, herm):
        nb = len(self.btd)
        nbm1 = nb - 1

        # First we need to calculate diagonal blocks of the spectral matrix
        blocks, A = self._green_diag_block(self.elecs_pvt_dev[elec].ravel())
        A = A @ self._data.gamma[elec] @ dagger(A)

        # Allocate space for the full matrix
        S = np.empty([len(self), len(self)], dtype=A.dtype)

        c = np.append(0, self.btd_cum)
        S[c[blocks[0]]:c[blocks[-1]+1], c[blocks[0]]:c[blocks[-1]+1]] = A
        del A

        # now loop backwards
        tX = self._data.tX
        tY = self._data.tY

        def left_calc(i, j, c, S, tY):
            """ Calculate the next block LEFT of block (i,j) """
            ij = slice(c[i], c[i+1]), slice(c[j], c[j+1])
            ijm1 = ij[0], slice(c[j-1], c[j])
            S[ijm1] = - S[ij] @ dagger(tY[j])

        def right_calc(i, j, c, S, tX):
            """ Calculate the next block RIGHT of block (i,j) """
            ij = slice(c[i], c[i+1]), slice(c[j], c[j+1])
            ijp1 = ij[0], slice(c[j+1], c[j+2])
            S[ijp1] = - S[ij] @ dagger(tX[j])

        def above_calc(i, j, c, S, tY):
            """ Calculate the next block ABOVE of block (i,j) """
            ij = slice(c[i], c[i+1]), slice(c[j], c[j+1])
            im1j = slice(c[i-1], c[i]), ij[1]
            S[im1j] = - tY[i] @ S[ij]

        def below_calc(i, j, c, S, tX):
            """ Calculate the next block BELOW of block (i,j) """
            ij = slice(c[i], c[i+1]), slice(c[j], c[j+1])
            ip1j = slice(c[i+1], c[i+2]), ij[1]
            S[ip1j] = - tX[i] @ S[ij]

        # define lefts
        if herm:
            def copy_herm(i, j, c, S):
                """ Copy block (j,i) to (i,j) """
                ij = slice(c[i], c[i+1]), slice(c[j], c[j+1])
                S[ij] = S[ij[1], ij[0]].T.conj()

            def left(i, j, a, b, c, S, tX, tY):
                if j <= 0:
                    return
                jm1 = j - 1
                if i >= jm1:
                    left_calc(i, j, c, S, tY)
                else:
                    copy_herm(i, jm1, c, S)
                left(i, jm1, a, b, c, S, tX, tY)
                if b:
                    below(i, jm1, c, S, tX)
                if a:
                    above(i, jm1, c, S, tY)

            def right(i, j, a, b, c, S, tX, tY):
                if nbm1 <= j:
                    return
                jp1 = j + 1
                if i >= jp1:
                    right_calc(i, j, c, S, tX)
                else:
                    copy_herm(i, jp1, c, S)
                if a:
                    above(i, jp1, c, S, tY)
                if b:
                    below(i, jp1, c, S, tX)
                right(i, jp1, a, b, c, S, tX, tY)

            def below(i, j, c, S, tX):
                if nbm1 <= i:
                    return
                ip1 = i + 1
                if ip1 >= j:
                    below_calc(i, j, c, S, tX)
                else:
                    copy_herm(ip1, j, c, S)
                below(ip1, j, c, S, tX)

            def above(i, j, c, S, tY):
                if i <= 0:
                    return
                im1 = i - 1
                if im1 >= j:
                    above_calc(i, j, c, S, tY)
                else:
                    copy_herm(im1, j, c, S)
                above(im1, j, c, S, tY)

        else:
            def left(i, j, a, b, c, S, tX, tY):
                if j <= 0:
                    return
                left_calc(i, j, c, S, tY)
                left(i, j-1, a, b, c, S, tX, tY)
                if b:
                    below(i, j-1, c, S, tX)
                if a:
                    above(i, j-1, c, S, tY)

            def right(i, j, a, b, c, S, tX, tY):
                if nbm1 <= j:
                    return
                right_calc(i, j, c, S, tX)
                right(i, j+1, a, b, c, S, tX, tY)
                if a:
                    above(i, j+1, c, S, tY)
                if b:
                    below(i, j+1, c, S, tX)

            def below(i, j, c, S, tX):
                if nbm1 <= i:
                    return
                below_calc(i, j, c, S, tX)
                below(i+1, j, c, S, tX)

            def above(i, j, c, S, tY):
                if i <= 0:
                    return
                above_calc(i, j, c, S, tY)
                above(i-1, j, c, S, tY)

        # start calculating
        if len(blocks) == 1:
            left(blocks[0], blocks[0], True, True, c, S, tX, tY)
            above(blocks[0], blocks[0], c, S, tY)
            below(blocks[0], blocks[0], c, S, tX)
            right(blocks[0], blocks[0], True, True, c, S, tX, tY)
        else:
            left(blocks[1], blocks[0], False, True, c, S, tX, tY)
            left(blocks[0], blocks[0], True, False, c, S, tX, tY)
            above(blocks[0], blocks[0], c, S, tY)
            above(blocks[0], blocks[1], c, S, tY)
            below(blocks[1], blocks[0], c, S, tX)
            below(blocks[1], blocks[1], c, S, tX)
            right(blocks[0], blocks[1], True, False, c, S, tX, tY)
            right(blocks[1], blocks[1], False, True, c, S, tX, tY)

        return S

    def _scattering_state_reduce(self, elec, DOS, U, cutoff):
        """ U on input is a fortran-index as returned from eigh or svd """
        # Select only the first N components where N is the
        # number of orbitals in the electrode (there can't be
        # any more propagating states anyhow).
        N = len(self._data.gamma[elec])

        # sort and take N highest values
        idx = np.argsort(-DOS)[:N]

        if cutoff > 0:
            # also retain values with large negative DOS.
            # These should correspond to states with large weight, but in some
            # way unphysical. The DOS *should* be positive.
            idx1 = (np.fabs(DOS[idx]) >= cutoff).nonzero()[0]
            idx = idx[idx1]

        return DOS[idx], U[:, idx]

    def scattering_state(self, elec, E, k=(0, 0, 0), cutoff=0., method='svd', *args, **kwargs):
        r""" Calculate the scattering states for a given `E` and `k` point from a given electrode

        The scattering states are the eigen states of the spectral function:

        .. math::
            \mathbf A_{\mathfrak{e}}(E,\mathbf k) \mathbf u = 2\pi\mathbf a \mathbf u

        where :math:`\mathbf a_i` is the DOS carried by the :math:`i`'th scattering
        state.

        Parameters
        ----------
        elec : str or int
           the electrode to calculate the spectral function from
        E : float
           the energy to calculate at, may be a complex value.
        k : array_like, optional
           k-point to calculate the spectral function at
        cutoff : float, optional
           cutoff the returned scattering states at some DOS value.
        method : {'svd', 'propagate', 'full'}
           which method to use for calculating the scattering states.
           Use only the _full_ method testing purposes as it is extremely slow
           and requires a substantial amount of memory.
           The SVD method is the fastests considering its complete precision.
           The propagate method may be even faster for very large systems with
           very little loss of precision, depends on `cutoff`.
        """
        elec = self._elec(elec)
        self._prepare(E, k)
        method = method.lower()
        func = getattr(self, f"_scattering_state_{method}", None)
        if func is None:
            raise ValueError(f"{self.__class__.__name__}.scattering_state method is not [full,svd,propagate]")
        return func(elec, cutoff, *args, **kwargs)

    def _scattering_state_full(self, elec, cutoff=0., **kwargs):
        # We know that scattering_state has called prepare!
        A = self.spectral(elec, self._data.E, self._data.k, **kwargs)

        # add something to the diagonal (improves diag precision for small states)
        np.fill_diagonal(A, A.diagonal() + 0.1)

        # Now diagonalize A
        DOS, A = eigh_destroy(A)
        # backconvert diagonal
        DOS -= 0.1
        # TODO check with overlap convert with correct magnitude (Tr[A] / 2pi)
        DOS /= 2 * np.pi
        DOS, A = self._scattering_state_reduce(elec, DOS, A, cutoff)

        data = self._data
        info = dict(
            method='full',
            elec=self._elec_name(elec),
            E=data.E,
            k=data.k,
            cutoff=cutoff
        )

        # always have the first state with the largest values
        return si.physics.StateCElectron(A.T, DOS, self, **info)

    def _scattering_state_svd(self, elec, cutoff=0., **kwargs):
        A = self._green_column(self.elecs_pvt_dev[elec].ravel())

        # This calculation uses the sqrt(Gamma) calculation combined with svd
        Gamma_sqrt = sqrtm(self._data.gamma[elec])
        A = A @ Gamma_sqrt

        # Perform svd
        DOS, A = _scat_state_svd(A, **kwargs)
        DOS, A = self._scattering_state_reduce(elec, DOS, A, cutoff)

        data = self._data
        info = dict(
            method='svd',
            elec=self._elec_name(elec),
            E=data.E,
            k=data.k,
            cutoff=cutoff
        )

        # always have the first state with the largest values
        return si.physics.StateCElectron(A.T, DOS, self, **info)

    def _scattering_state_propagate(self, elec, cutoff=0, **kwargs):
        # Parse the cutoff value
        # Here we may use 2 values, one for cutting off the initial space
        # and one for the returned space.
        cutoff = np.array(cutoff).ravel()
        if cutoff.size != 2:
            cutoff0 = cutoff1 = cutoff[0]
        else:
            cutoff0, cutoff1 = cutoff[0], cutoff[1]

        # First we need to calculate diagonal blocks of the spectral matrix
        # This is basically the same thing as calculating the Gf column
        # But only in the 1/2 diagonal blocks of Gf
        blocks, U = self._green_diag_block(self.elecs_pvt_dev[elec].ravel())

        # Calculate the spectral function only for the blocks that host the
        # scattering matrix
        U = U @ self._data.gamma[elec] @ dagger(U)

        # add something to the diagonal (improves diag precision)
        np.fill_diagonal(U, U.diagonal() + 0.1)

        # Calculate eigenvalues
        DOS, U = eigh_destroy(U)
        # backconvert diagonal
        DOS -= 0.1
        # TODO check with overlap convert with correct magnitude (Tr[A] / 2pi)
        DOS /= 2 * np.pi

        # Remove states for cutoff and size
        # Since there cannot be any addition of states later, we
        # can do the reduction here.
        # This will greatly increase performance for very wide systems
        # since the number of contributing states is generally a fraction
        # of the total electrode space.
        DOS, U = self._scattering_state_reduce(elec, DOS, U, cutoff0)
        # Back-convert to retain scale of the vectors before SVD
        # and also take the sqrt to ensure U U^dagger returns
        # a sensible value.
        U *= signsqrt(DOS * 2 * np.pi)

        nb = len(self.btd)

        u = [None] * nb
        u[blocks[0]] = U[:self.btd[blocks[0]], :]
        if len(blocks) > 1:
            u[blocks[1]] = U[self.btd[blocks[0]]:, :]

        # Clean up
        del U

        # Propagate U in the full BTD matrix
        t = self._data.tY
        for b in range(blocks[0], 0, -1):
            u[b - 1] = - t[b] @ u[b]

        t = self._data.tX
        for b in range(blocks[-1], nb - 1):
            u[b + 1] = - t[b] @ u[b]

        # Now the full U is created (still F-order)
        u = np.concatenate(u)

        # Perform svd
        DOS, u = _scat_state_svd(u, **kwargs)

        # TODO check with overlap convert with correct magnitude (Tr[A] / 2pi)
        DOS, u = self._scattering_state_reduce(elec, DOS, u, cutoff1)

        # Now we have the full u, create it and transpose to get it in C indexing
        data = self._data
        info = dict(
            method='propagate',
            elec=self._elec_name(elec),
            E=data.E,
            k=data.k,
            cutoff_space=cutoff0,
            cutoff=cutoff1
        )
        return si.physics.StateCElectron(u.T, DOS, self, **info)

    def eigenchannel(self, state, elec_to):
        r""" Calculate the eigen channel from scattering states entering electrodes `elec_to`

        The energy and k-point is inferred from the `state` object and it should have
        been a returned value from `scattering_state`.

        The eigenchannels are the eigen states of the transmission matrix in the
        energy weighted scattering states:

        .. math::
            \mathbf A_{\mathfrak{e}}(E,\mathbf k) \mathbf u &= 2\pi\mathbf a \mathbf u
            \\
            \mathbf t_{\mathbf u} &= \sum \langle \mathbf u | \boldsymbol\Gamma_{\mathfrak{e\to}} | \mathbf u\rangle

        where the eigenvectors of :math:`\mathbf t_{\mathbf u}` is the coefficients of the
        scattering states for the individual eigen channels. The eigenvalues are the
        transmission eigenvalues.
        """
        self._prepare_se(state.info["E"], state.info["k"])
        if isinstance(elec_to, (Integral, str, PivotSelfEnergy)):
            elec_to = [elec_to]
        # convert to indices
        elec_to = [self._elec(e) for e in elec_to]

        # The sign shouldn't really matter since the states should always
        # have a finite DOS, however, for completeness sake we retain the sign.
        # We scale the vectors by sqrt(DOS/2pi).
        # This is because the scattering states from self.scattering_state
        # stores eig(A) / 2pi.
        sqDOS = signsqrt(state.c).reshape(-1, 1)
        # Retrive the scattering states `A` and apply the proper scaling
        # We need this scaling for the eigenchannel construction anyways.
        A = state.state * sqDOS

        # create shorthands
        elec_pvt_dev = self.elecs_pvt_dev
        G = self._data.gamma

        # Create the first electrode
        el = elec_to[0]
        idx = elec_pvt_dev[el].ravel()
        u = A[:, idx]
        # the summed transmission matrix
        Ut = u.conj() @ G[el] @ u.T
        for el in elec_to[1:]:
            idx = elec_pvt_dev[el].ravel()
            u = A[:, idx]
            Ut += u.conj() @ G[el] @ u.T

        # TODO currently a factor depends on what is used
        #      in `scattering_states`, so go check there.
        #      The resulting Ut should have a factor: 1 / 2pi ** 0.5
        #      When the states DOS values (`state.c`) has the factor 1 / 2pi
        #      then `u` has the correct magnitude and all we need to do is to add the factor 2pi
        # diagonalize the transmission matrix tt
        tt, Ut = eigh_destroy(Ut)
        tt *= 2 * np.pi

        info = {**state.info}
        info["elec_to"] = [self._elec_name(e) for e in elec_to]

        # Backtransform U to form the eigenchannels
        return si.physics.StateCElectron(Ut[:, ::-1].T @ A,
                                         tt[::-1], self, **info)
