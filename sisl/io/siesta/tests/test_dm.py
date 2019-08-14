""" pytest test configures """
from __future__ import print_function

import pytest
import numpy as np

import sisl


pytestmark = [pytest.mark.io, pytest.mark.siesta]
_dir = 'sisl/io/siesta'


def test_si_pdos_kgrid_dm(sisl_files):
    fdf = sisl.get_sile(sisl_files(_dir, 'si_pdos_kgrid.fdf'), base=sisl_files(_dir))

    si = sisl.get_sile(sisl_files(_dir, 'si_pdos_kgrid.DM'))

    DM1 = si.read_density_matrix(geometry=fdf.read_geometry())
    DM2 = fdf.read_density_matrix(order=['DM'])

    assert DM1._csr.spsame(DM2._csr)
    assert np.allclose(DM1._csr._D[:, :-1], DM2._csr._D[:, :-1])


def test_si_pdos_kgrid_dm_mulliken(sisl_files):
    fdf = sisl.get_sile(sisl_files(_dir, 'si_pdos_kgrid.fdf'), base=sisl_files(_dir))
    DM = fdf.read_density_matrix(order=['DM'])

    Mo = DM.mulliken('orbital')
    Ma = DM.mulliken('atom')

    o2a = DM.geometry.o2a(np.arange(DM.no))

    ma = np.zeros_like(Ma.T)
    np.add.at(ma, o2a, Mo.T)
    assert np.allclose(ma.T, Ma)
