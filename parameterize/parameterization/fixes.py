# (c) 2015-2018 Acellera Ltd http://www.acellera.com
# All Rights Reserved
# Distributed under HTMD Software License Agreement
# No redistribution in whole or part
#
import logging
import os

import networkx as nx

logger = logging.getLogger(__name__)


def _getMolecularGraph(molecule):
    """
    Generate a graph from the topology of molecule
    """

    graph = nx.Graph()
    for i in range(molecule.numAtoms):
        graph.add_node(i, element=molecule.element[i])
    for i, bond in enumerate(molecule.bonds):
        graph.add_edge(*bond, index=i)

    return graph


def fixPhosphateTypes(molecule):
    """
    >>> from parameterize.home import home
    >>> from moleculekit.molecule import Molecule
    >>> from parameterize.charge import fitGasteigerCharges

    >>> mol = Molecule(os.path.join(home('test-param'), '1a1e_ligand.mol2'))

    >>> fitGasteigerCharges(mol) # doctest: +ELLIPSIS
    Traceback (most recent call last):
    ...
    RuntimeError: RDKit error
    [...]     MOL: warning - O.co2 with non C.2 or S.o2 neighbor.
    <BLANKLINE>
    <BLANKLINE>

    >>> new_mol = fixPhosphateTypes(mol)

    >>> print(new_mol.atomtype)
    ['C.2' 'O.2' 'C.3' 'N.am' 'C.3' 'C.2' 'O.2' 'C.3' 'C.ar' 'C.ar' 'C.ar'
     'C.ar' 'C.ar' 'C.ar' 'O.3' 'P.3' 'O.3' 'O.3' 'O.2' 'N.am' 'C.3' 'C.2'
     'O.2' 'C.3' 'C.3' 'C.2' 'O.co2' 'O.co2' 'N.am' 'C.3' 'C.3' 'C.3' 'C.3'
     'C.3' 'C.3' 'C.3' 'C.3' 'C.3' 'H' 'H' 'H' 'H' 'H' 'H' 'H' 'H' 'H' 'H' 'H'
     'H' 'H' 'H' 'H' 'H' 'H' 'H' 'H' 'H' 'H' 'H' 'H' 'H' 'H' 'H' 'H' 'H' 'H'
     'H' 'H' 'H' 'H' 'H' 'H']

    >>> print(new_mol.bondtype) # doctest: +NORMALIZE_WHITESPACE
    ['2' '1' '1' '1' '1' '2' '1' 'ar' 'ar' 'ar' 'ar' '1' 'ar' 'ar' '1' '2' '1'
     '1' '1' '1' '1' '1' '1' 'ar' 'ar' '2' '1' '1' '1' '1' '1' '1' '1' '1' '1'
     '1' 'am' 'am' 'am' '1' '1' '1' '1' '1' '1' '1' '1' '1' '1' '1' '1' '1' '1'
     '1' '1' '1' '1' '1' '1' '1' '1' '1' '1' '1' '1' '1' '1' '1' '1' '1' '1'
     '1' '1' '1']

    >>> print(round(sum(fitGasteigerCharges(new_mol).charge)))
    -3.0

    >>> mol = Molecule(os.path.join(home('test-param'), '1afk_ligand.mol2'))
    >>> new_mol = fixPhosphateTypes(mol)

    >>> print(new_mol.atomtype)
    ['P.3' 'O.3' 'O.3' 'O.2' 'P.3' 'O.3' 'O.2' 'O.3' 'O.3' 'C.3' 'C.3' 'O.3'
     'C.3' 'O.3' 'P.3' 'O.3' 'O.3' 'O.2' 'C.3' 'O.3' 'C.3' 'N.pl3' 'C.2' 'N.2'
     'C.ar' 'C.ar' 'N.pl3' 'N.ar' 'C.ar' 'N.ar' 'C.ar' 'H' 'H' 'H' 'H' 'H' 'H'
     'H' 'H' 'H' 'H' 'H']

    >>> print(new_mol.bondtype)
    ['1' '2' '1' '1' '1' '1' '2' '1' '1' '1' '1' '1' '1' '1' '1' '1' '1' '1'
     '1' '2' '1' '1' '1' '1' '2' '1' 'ar' 'ar' 'ar' '1' 'ar' 'ar' 'ar' '1' '1'
     '1' '1' '1' '1' '1' '1' '1' '1' '1']

    >>> print(round(sum(fitGasteigerCharges(new_mol).charge)))
    -5.0

    >>> mol = Molecule(os.path.join(home('test-param'), '1aj7_ligand.mol2'))
    >>> new_mol = fixPhosphateTypes(mol)

    >>> print(new_mol.atomtype)
    ['C.ar' 'N.2' 'O.2' 'O.2' 'C.ar' 'C.ar' 'C.ar' 'C.ar' 'C.ar' 'O.3' 'P.3'
     'O.3' 'O.2' 'C.3' 'C.3' 'C.3' 'C.3' 'C.2' 'O.co2' 'O.co2' 'H' 'H' 'H' 'H'
     'H' 'H' 'H' 'H' 'H' 'H' 'H' 'H']

    >>> print(new_mol.bondtype)
    ['ar' 'ar' '1' '2' '2' 'ar' 'ar' '1' 'ar' 'ar' '1' '1' '2' '1' '1' '1' '1'
     '1' 'ar' 'ar' '1' '1' '1' '1' '1' '1' '1' '1' '1' '1' '1' '1']

    >>> print(round(sum(fitGasteigerCharges(new_mol).charge)))
    -2.0
    """

    molecule = molecule.copy()
    graph = _getMolecularGraph(molecule)

    for node in graph.nodes:

        # Skip not P atoms
        if graph.nodes[node]["element"] != "P":
            continue

        # Skip P atom without 4 atoms connected
        neighbors = list(graph.neighbors(node))
        if len(neighbors) != 4:
            continue

        # Filter O atoms
        is_oxygen = lambda node: graph.nodes[node]["element"] == "O"
        neighbors = filter(is_oxygen, neighbors)

        # Sort O atoms according to descending charge
        # Note: a double bond has to be near the most positive oxygen
        charge = lambda neighbor: molecule.charge[neighbor]
        neighbors = sorted(neighbors, key=charge, reverse=True)

        num_double = 0

        # Iterate O atoms
        for neighbor in neighbors:
            assert graph.nodes[neighbor]["element"] == "O"

            # Get O atom and P--O bond type
            num_bonds = len(list(graph.neighbors(neighbor)))
            if num_bonds == 2:
                new_atom = "O.3"
                new_bond = "1"
            elif num_bonds == 1 and num_double == 0:
                new_atom = "O.2"
                new_bond = "2"
                num_double += 1
            elif num_bonds == 1 and num_double == 1:
                new_atom = "O.3"
                new_bond = "1"
            else:
                raise ValueError()

            # Change the O atom type
            old_atom = molecule.atomtype[neighbor]
            if old_atom != new_atom:
                molecule.atomtype[neighbor] = new_atom
                logger.info(
                    "Change atom {} type: {} --> {}".format(
                        neighbor, old_atom, new_atom
                    )
                )

            # Change the P--O bond type
            bond_index = graph.edges[(node, neighbor)]["index"]
            old_bond = molecule.bondtype[bond_index]
            if old_bond != new_bond:
                molecule.bondtype[bond_index] = new_bond
                logger.info(
                    "Change bond {}--{} type: {} --> {}".format(
                        *molecule.bonds[bond_index], old_bond, new_bond
                    )
                )

    return molecule


if __name__ == "__main__":

    from moleculekit.molecule import Molecule
    from parameterize.charge import fitGasteigerCharges

    import sys
    import doctest

    # Prevent HTMD importing inside doctest to fail if importing gives text output
    from parameterize.home import home

    home()

    sys.exit(doctest.testmod().failed)
