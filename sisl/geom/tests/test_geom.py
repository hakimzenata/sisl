# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
from functools import partial
import pytest

from sisl import Atom
from sisl.geom import *

import itertools
import math as m
import numpy as np


pytestmark = [pytest.mark.geom]


def test_basis():
    a = sc(2.52, Atom['Fe'])
    a = bcc(2.52, Atom['Fe'])
    a = bcc(2.52, Atom['Fe'], orthogonal=True)
    a = fcc(2.52, Atom['Au'])
    a = fcc(2.52, Atom['Au'], orthogonal=True)
    a = hcp(2.52, Atom['Au'])
    a = hcp(2.52, Atom['Au'], orthogonal=True)


def test_flat():
    a = graphene()
    a = graphene(atoms='C')
    a = graphene(orthogonal=True)


def test_nanotube():
    a = nanotube(1.42)
    a = nanotube(1.42, chirality=(3, 5))
    a = nanotube(1.42, chirality=(6, -3))


def test_diamond():
    a = diamond()


def test_bilayer():
    a = bilayer(1.42)
    a = bilayer(1.42, stacking='AA')
    a = bilayer(1.42, stacking='BA')
    a = bilayer(1.42, stacking='AB')
    for m in range(7):
        a = bilayer(1.42, twist=(m, m + 1))
    a = bilayer(1.42, twist=(6, 7), layer='bottom')
    a = bilayer(1.42, twist=(6, 7), layer='TOP')
    a = bilayer(1.42, bottom_atoms=(Atom['B'], Atom['N']), twist=(6, 7))
    a = bilayer(1.42, top_atoms=(Atom(5), Atom(7)), twist=(6, 7))
    a, th = bilayer(1.42, twist=(6, 7), ret_angle=True)

    with pytest.raises(ValueError):
        bilayer(1.42, twist=(6, 7), layer='undefined')

    with pytest.raises(ValueError):
        bilayer(1.42, twist=(6, 7), stacking='undefined')

    with pytest.raises(ValueError):
        bilayer(1.42, twist=('str', 7), stacking='undefined')


def test_nanoribbon():
    for w in range(0, 5):
        a = nanoribbon(1.42, Atom(6), w, kind='armchair')
        a = nanoribbon(1.42, Atom(6), w, kind='zigzag')
        a = nanoribbon(1.42, (Atom(5), Atom(7)), w, kind='armchair')
        a = nanoribbon(1.42, (Atom(5), Atom(7)), w, kind='zigzag')

    with pytest.raises(ValueError):
        nanoribbon(1.42, (Atom(5), Atom(7)), 6, kind='undefined')

    with pytest.raises(ValueError):
        nanoribbon(1.42, (Atom(5), Atom(7)), 'str', kind='undefined')


def test_graphene_nanoribbon():
    a = graphene_nanoribbon(5)


def test_agnr():
    a = agnr(5)


def test_zgnr():
    a = zgnr(5)


def test_heteroribbon():
    """Runs the heteroribbon builder for all possible combinations of
    widths and asserts that they are always properly aligned.
    """
    # Build combinations
    combinations = itertools.product([7, 8, 9, 10, 11], [7, 8, 9, 10, 11])
    L = itertools.repeat(2)

    for Ws in combinations:
        geom = heteroribbon(zip(Ws, L), bond=1.42, atoms=Atom(6, 1.43), align="auto", shift_quantum=True)

        # Assert no dangling bonds.
        assert len(geom.asc2uc({"neighbours": 1})) == 0


def test_graphene_heteroribbon():
    a = graphene_heteroribbon([(7, 2), (9, 2)])


def test_graphene_heteroribbon_errors():


    # 7-open with 9 can only be perfectly aligned.
    graphene_heteroribbon([(7,1), (9,1)], align="center", on_lone_atom="raise")
    with pytest.raises(ValueError):
        graphene_heteroribbon([(7,1), (9,1,-1)], align="center", on_lone_atom="raise")

    grap_heteroribbon = partial(
        graphene_heteroribbon, align="auto", shift_quantum=True
    )

    # Odd section with open end
    with pytest.raises(ValueError):
        grap_heteroribbon([(7, 3), (5, 2)])

    # Shift limits are imposed correctly
    # In this case -2 < shift < 1
    grap_heteroribbon([(7, 3), (11, 2, 0)])
    grap_heteroribbon([(7, 3), (11, 2, -1)])
    with pytest.raises(ValueError):
        grap_heteroribbon([(7, 3), (11, 2, 1)])
    with pytest.raises(ValueError):
        grap_heteroribbon([(7, 3), (11, 2, -2)])

    # Periodic boundary conditions work properly
    # grap_heteroribbon([[10, 2], [8, 1, 0]], pbc=False)
    # with pytest.raises(ValueError):
    #     grap_heteroribbon([[10, 2], [8, 1, 0]], pbc=True)

    # Even ribbons should only be shifted towards the center
    grap_heteroribbon([(10, 2), (8, 2, -1)])
    with pytest.raises(ValueError):
        grap_heteroribbon([(10, 2), (8, 2, 1)])
    grap_heteroribbon([(10, 1), (8, 2, 1)],) #pbc=False)
    with pytest.raises(ValueError):
        grap_heteroribbon([(10, 1), (8, 2, -1)],) #pbc=False)
