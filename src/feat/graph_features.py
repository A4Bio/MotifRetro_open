# -*- coding: utf-8 -*-
"""
Atom and Bond features used in graph reaction representations
"""

import logging
from typing import List, Optional
from rdkit.Chem import AllChem

from rdkit.Chem.rdchem import Bond, Atom

logger = logging.getLogger(__name__)

# # values found on train+valid on USPTO-50k + USPTO-FULL + USPTO-MIT
# ATOM_PROPS = {
#     'atomic_num': [1, 3, 5, 6, 7, 8, 9, 11, 12, 13, 14, 15, 16, 17, 19, 22,
#                    29, 30, 32, 33, 34, 35, 47, 50, 51, 52, 53, 82, 83],
#     'formal_charge': [-1, 0, 1, 2, 3],
#     'chiral_tag': [0, 1, 2],
#     'num_explicit_hs': [0, 1, 2, 4],
#     'is_aromatic': [0, 1],
#     'is_supernode': [0, 1],  #
#     'is_edited': [0, 1],  # we mark atoms that have been added/edited by the model
#     'is_reactant': [0, 1]  # this feature is used to mark reactants in "SEPARATED" variant of forward prediction
# }

# BOND_PROPS = {
#     'bond_type': ['self', 'supernode', 1, 2, 3, 12],
#     'bond_stereo': [0, 2, 3],
#     'is_aromatic': [0, 1],
#     'is_edited': [0, 1],
# }

# ATOM_PROP2OH = dict((k, (dict((ap, i + 1) for i, ap in enumerate(vals)))) for k, vals in ATOM_PROPS.items())
# BOND_PROP2OH = dict((k, (dict((ap, i + 1) for i, ap in enumerate(vals)))) for k, vals in BOND_PROPS.items())

def bond_length(bond):
    """Bond length"""
    mol = bond.GetOwningMol()
    AllChem.Compute2DCoords(mol)
    if mol.GetNumConformers() == 0:
        mol.Compute2DCoords()
    conformer = mol.GetConformer()
    h = conformer.GetAtomPosition(bond.GetBeginAtomIdx())
    t = conformer.GetAtomPosition(bond.GetEndAtomIdx())
    return h.Distance(t)

def try_get_bond_feature(bond: Bond, feat_key: str):
    try:
        if feat_key == 'bond_type':
            return int(bond.GetBondType())
        elif feat_key == 'is_edited':
            if bond.HasProp('is_edited') and bond.GetBoolProp('is_edited'):
                return 1
            return 0
        elif feat_key == 'bond_stereo':
            return int(bond.GetStereo())
        elif feat_key == 'is_aromatic':
            return int(bond.GetIsAromatic())
        elif feat_key == 'is_conjugated': # new_feats
            return int(bond.GetIsConjugated())
        elif feat_key == 'bond_length': # new_feats
            return bond_length(bond)
        else:
            raise KeyError(f"Unknown bond feature: {feat_key}")
    except RuntimeError as e:
        logger.warning(f'Runtime error while try_get_bond_feature: {str(e)}')
        return None


def try_get_atom_feature(atom: Atom, feat_key: str):
    try:
        if feat_key == 'is_supernode':
            return 0
        elif feat_key == 'is_product':
            return 0
        elif feat_key == 'is_edited':
            if atom.HasProp('is_edited') and atom.GetBoolProp('is_edited'):
                return 1
            return 0
        elif feat_key == 'is_reactant':
            if atom.HasProp('in_target') and atom.GetBoolProp('in_target'):
                return 1
            return 0
        elif feat_key == 'atomic_num':
            return atom.GetAtomicNum()
        elif feat_key == 'chiral_tag':
            return int(atom.GetChiralTag())
        elif feat_key == 'formal_charge':
            return atom.GetFormalCharge()
        elif feat_key == 'is_aromatic':
            return int(atom.GetIsAromatic())
        elif feat_key == 'num_explicit_hs':
            return atom.GetNumExplicitHs()
        elif feat_key == 'radical_electrons': # new feats
            return atom.GetNumRadicalElectrons()
        elif feat_key == 'total_num_hs': # new feats
            return atom.GetTotalNumHs()
        elif feat_key == 'total_valence': # new feats
            return atom.GetTotalValence()
        elif feat_key == 'hybridization': # new feats
            return atom.GetHybridization()
        elif feat_key == 'is_in_ring': # new feats
            return atom.IsInRing()
        elif feat_key == 'is_in_ring3': # new feats
            return atom.IsInRingSize(3)
        elif feat_key == 'is_in_ring4': # new feats
            return atom.IsInRingSize(4)
        elif feat_key == 'is_in_ring5': # new feats
            return atom.IsInRingSize(5)
        elif feat_key == 'is_in_ring6': # new feats
            return atom.IsInRingSize(6)
        else:
            raise KeyError(f"Unknown atom feature: {feat_key}")
    except RuntimeError as e:
        logger.warning(f'Runtime error while try_get_atom_feature: {str(e)}')
        return None


def get_atom_features(atom: Atom, atom_oh_keys: List[str], used_oh_keys: Optional[List[str]] = None,
                      atom_prop2oh: dict = None) -> List[int]:
    feat = [try_get_atom_feature(atom, key) if used_oh_keys is None or key in used_oh_keys else 0
            for key in atom_oh_keys]
    result = []
    for key, val in zip(atom_oh_keys, feat):
        if key not in atom_prop2oh:
            continue
        if val not in atom_prop2oh[key]:
            raise ValueError(f'Unknown {key} value: {val}')
            # # --------临时修复
            # if key == 'num_explicit_hs' and val == 3: # 临时修复 TODO: 需要把 num_explicit_hs = 3 的情况加入 feat_vocab 中。
            #     result.append(0)
            # else:
            #     raise ValueError(f'Unknown {key} value: {val}')
        else:
            result.append(atom_prop2oh[key][val])
    return result


def get_bond_features(bond: Bond, bond_oh_keys: List[str], used_oh_keys: Optional[List[str]] = None,
                      bond_prop2oh: dict = None) -> List[int]:
    feat = [try_get_bond_feature(bond, key) if used_oh_keys is None or key in used_oh_keys else 0
            for key in bond_oh_keys]
    result = []
    for key, val in zip(bond_oh_keys, feat):
        if key not in bond_prop2oh:
            continue
        if key == 'bond_length':
            result.append(val)
        elif val not in bond_prop2oh[key]:
            logger.debug(f'Unknown {key} value: {val}')
            result.append(0)
        else:
            result.append(bond_prop2oh[key][val])
    return result
