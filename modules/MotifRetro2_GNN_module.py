"""
Decoder of MEGAN model.
Outputs final action probabilities for atoms and bonds
"""
from typing import Tuple, List

import torch
from torch import nn
import torch.nn.functional as F
from torch_scatter import scatter_softmax, scatter_sum
import torch.jit as jit


device = 'cuda:0' if torch.cuda.is_available() else 'cpu'


def softmax(values, base, dim):
    exp = torch.exp(base * values)
    return exp / torch.sum(exp, dim=dim).unsqueeze(-1)


class MultiHeadGraphConvLayer(nn.Module):
    def __init__(self, hidden_dim: int, input_dim: int, output_dim: int, residual: bool, att_heads: int = 8, att_dim: int = 64, attention_dropout=0.0):
        """
        :param att_dim: dimensionality of narrowed nodes representation for the attention
        :param att_heads: number of attention heads
        """
        super(MultiHeadGraphConvLayer, self).__init__()
        self.n_att = att_heads
        self.att_dim = att_dim

        # self.atoms_att = nn.Linear(input_dim, att_dim)
        self.v_layer = nn.Linear(input_dim, int(input_dim / att_heads))
        self.final_att = nn.Sequential(
            nn.Linear(input_dim * 2 + input_dim, input_dim),
            nn.ReLU(),
            nn.Linear(input_dim, output_dim),
            nn.ReLU(),
            nn.Linear(output_dim, att_heads))
        
        

        self.conv_layer = nn.Linear(input_dim, output_dim)
        self.motif_gate = nn.Linear(input_dim, output_dim)
        self.bond_layer = nn.Linear(input_dim + hidden_dim, hidden_dim)

        self.num_embed = nn.Linear(1, output_dim)
        self.dropout = nn.Dropout(attention_dropout)

        if output_dim % att_heads != 0:
            raise ValueError(f"Output dimension ({output_dim} "
                             f"must be a multiple of number of attention heads ({att_heads}")

        self.residual = residual

    def forward(self, atom_feat, bond_feat, edge_idx, graph_id=None, apply_activation: bool = True):
        # x_att = torch.relu(self.atoms_att(atom_feat))  # [N, att_dim]
        x_att = torch.cat([atom_feat[edge_idx[:,1]], atom_feat[edge_idx[:,0]], bond_feat], dim=-1)  # [E, 2 * att_dim + bond_dim]
        x_att = self.final_att(x_att)  # [E, att_heads]
        src, dst = edge_idx[:,0], edge_idx[:,1]  # [E], [E]
        att = scatter_softmax(x_att, src, dim=0) # 在evaluation时这一行可能报错
        
        out = scatter_sum((att.unsqueeze(1) * self.v_layer(atom_feat)[dst].unsqueeze(2)), src, dim=0, dim_size=atom_feat.shape[0])
        out = self.dropout(out)
        out = self.conv_layer(out.view(out.shape[0], -1))

        new_bond_feat = torch.cat([out[edge_idx[:,1]] + out[edge_idx[:,0]], bond_feat], dim=-1)
        new_bond_feat = torch.relu(self.bond_layer(new_bond_feat))

        if self.residual:
            out = atom_feat + out

        if apply_activation:
            out = torch.relu(out)

        return out, new_bond_feat


class GGTU(nn.Module):
    def __init__(self, bond_dim, hidden_dim, att_heads, att_dim, attention_dropout) -> None:
        super().__init__()
        self.GNN_update = MultiHeadGraphConvLayer(
                                            hidden_dim=hidden_dim,
                                            input_dim=hidden_dim*3,
                                            output_dim=hidden_dim, residual=False, 
                                            att_heads=att_heads, att_dim=att_dim, attention_dropout=attention_dropout)
        
        self.GNN_reset = MultiHeadGraphConvLayer(
                                            hidden_dim=hidden_dim,
                                            input_dim=hidden_dim*3,
                                            output_dim=hidden_dim, residual=False, 
                                            att_heads=att_heads, att_dim=att_dim, attention_dropout=attention_dropout)
        
        self.MLP_x = nn.Sequential(nn.Linear(hidden_dim*3, hidden_dim))
        self.MLP_x_e = nn.Sequential(nn.Linear(hidden_dim*3, hidden_dim))
        
        self.MLP_h = nn.Sequential(nn.Linear(hidden_dim*3, hidden_dim))
        self.MLP_h_e = nn.Sequential(nn.Linear(hidden_dim*3, hidden_dim))
        
        self.MLP_m = nn.Sequential(nn.Linear(hidden_dim*3, hidden_dim))
        self.MLP_m_e = nn.Sequential(nn.Linear(hidden_dim*3, hidden_dim))
        
        
        
    def forward(self, x, m, h, x_e, m_e, h_e, edge_idx, graph_id=None):
        m = torch.zeros_like(x) if m is None else m
        h = torch.zeros_like(x) if h is None else h
        m_e = torch.zeros_like(x_e) if m_e is None else m_e
        h_e = torch.zeros_like(x_e) if h_e is None else h_e
        feat_node = torch.cat([x,m,h], dim=-1)
        feat_edge = torch.cat([x_e,m_e,h_e], dim=-1)
        
        z, z_e = self.GNN_update(feat_node, feat_edge, edge_idx, graph_id=graph_id)
        r, r_e = self.GNN_reset(feat_node, feat_edge, edge_idx, graph_id=graph_id)
        z, z_e, r, r_e = torch.sigmoid(z), torch.sigmoid(z_e), torch.sigmoid(r), torch.sigmoid(r_e)
        
        
        x_tile = self.MLP_x(torch.cat([r*x, m, h], dim=-1))
        x_e_tile = self.MLP_x_e(torch.cat([r_e*x_e, m_e, h_e], dim=-1))
        x = (1-z)*x + z*x_tile
        x_e = (1-z_e)*x_e + z_e*x_e_tile
        
        h_tile = self.MLP_h(torch.cat([r*h, m, x], dim=-1))
        h_e_tile = self.MLP_h_e(torch.cat([r_e*h_e, m_e, x_e], dim=-1))
        h = (1-z)*h + z*h_tile
        h_e = (1-z_e)*h_e + z_e*h_e_tile
        
        m_tile = self.MLP_m(torch.cat([r*m, x, h], dim=-1))
        m_e_tile = self.MLP_m_e(torch.cat([r_e*m_e, x_e, h_e], dim=-1))
        m = (1-z)*m + z*m_tile
        m_e = (1-z_e)*m_e + z_e*m_e_tile
        
        return x, m, h, x_e, m_e, h_e
    

class MeganEncoder(nn.Module):
    def __init__(self, hidden_dim: int, bond_emb_dim: int, n_encoder_conv: int = 4,
                 enc_residual: bool = True, enc_dropout: float = 0.0, level: str = 'atom', att_heads=8, att_dim=128, use_motif_feature=0,
                 attention_dropout: float = 0.0):
        super(MeganEncoder, self).__init__()
        self.n_conv = n_encoder_conv
        self.residual = enc_residual
        self.dropout = nn.Dropout(enc_dropout) if enc_dropout > 0 else lambda x: x
        self.level = level
        self.use_motif_feature = use_motif_feature

        self.conv_layers = []
        

        for i in range(self.n_conv):
            conv = GGTU(bond_dim=bond_emb_dim, 
                        hidden_dim=hidden_dim,
                        att_heads=att_heads, 
                        att_dim=att_dim, 
                        attention_dropout=attention_dropout)
            self.conv_layers.append(conv)
            setattr(self, f'MultiHeadGraphConv_{i + 1}', conv)
        

    
    def forward(self, x, m, x_e, m_e, hidden_states, edge_idx):
        new_hidden_states = {}
        for i, conv in enumerate(self.conv_layers):
            if hidden_states is not None:
                h, h_e = hidden_states[i]
            else:
                h, h_e = None, None
            x, m, h, x_e, m_e, h_e = conv(x, m, h, x_e, m_e, h_e, edge_idx)
            new_hidden_states[i] = (h, h_e)
        return x, m, x_e, m_e, new_hidden_states
    


class MeganDecoder(nn.Module):
    def __init__(self, hidden_dim: int, bond_emb_dim, n_atom_actions: int, n_bond_actions: int, feat_vocab: dict,
                 n_fc: int = 3, n_decoder_conv: int = 4, dec_residual: bool = True, bond_atom_dim: int = 32,
                 atom_fc_hidden_dim: int = 256, bond_fc_hidden_dim: int = 256, dec_dropout: float = 0.0,
                 dec_hidden_dim: int = 0, dec_att_heads: int = 0, use_motif_action: bool = False,
                 predict_atom_num: bool = False, att_dim=128, att_heads = 8, attention_dropout = 0.1, temperature = 1.0):
        super(MeganDecoder, self).__init__()
        if dec_hidden_dim == 0:
            dec_hidden_dim = hidden_dim

        self.n_actions = n_atom_actions
        self.n_bond_actions = n_bond_actions
        self.feat_vocab = feat_vocab
        self.use_motif_action = use_motif_action
        self.predict_atom_num = predict_atom_num

        self.hidden_dim = hidden_dim
        self.n_fc = n_fc
        self.n_conv = n_decoder_conv
        self.residual = dec_residual
        self.atom_fc_hidden_dim = atom_fc_hidden_dim
        self.bond_fc_hidden_dim = bond_fc_hidden_dim
        self.bond_atom_dim = bond_atom_dim
        self.dropout = nn.Dropout(dec_dropout) if dec_dropout > 0 else lambda x: x
        self.temperature = temperature

        self.fc_atom_layers = []
        self.fc_bond_layers = []


        self.conv_layers = []

        self.atom_num_layer = nn.Sequential(
            nn.Linear(dec_hidden_dim, dec_hidden_dim),
            nn.Tanh(),
            nn.Dropout(p=0.1),
            nn.Linear(dec_hidden_dim, 50),
        )

        for i in range(self.n_conv):
            input_dim = hidden_dim if i == 0 else dec_hidden_dim
            output_dim = hidden_dim if i == self.n_conv - 1 else dec_hidden_dim
            if dec_att_heads == 0:
                conv = MultiHeadGraphConvLayer(hidden_dim=hidden_dim, input_dim=input_dim, output_dim=output_dim,residual=False, att_dim=att_dim, attention_dropout=attention_dropout)
            else:
                conv = MultiHeadGraphConvLayer(hidden_dim=hidden_dim, input_dim=input_dim, output_dim=output_dim,residual=False, att_heads=dec_att_heads, att_dim=att_dim, attention_dropout=attention_dropout)

            self.conv_layers.append(conv)
            setattr(self, f'MultiHeadGraphConv_{i + 1}', conv)

        ff_hidden_dim = hidden_dim

        for i in range(n_fc):
            in_dim = ff_hidden_dim if i == 0 else atom_fc_hidden_dim
            out_dim = atom_fc_hidden_dim if i < n_fc - 1 else n_atom_actions

            atom_fc = nn.Linear(in_dim, out_dim)
            setattr(self, f'fc_atom_{i + 1}', atom_fc)
            self.fc_atom_layers.append(atom_fc)

        self.fc_atom_proj = nn.Linear(ff_hidden_dim, bond_atom_dim)
        self.fc_bond_proj = nn.Linear(ff_hidden_dim, bond_atom_dim)

        for i in range(n_fc):
            # in_dim = 2 * bond_atom_dim + bond_emb_dim if i == 0 else bond_fc_hidden_dim
            in_dim = bond_atom_dim + bond_emb_dim if i == 0 else bond_fc_hidden_dim
            out_dim = bond_fc_hidden_dim if i < n_fc - 1 else n_bond_actions

            bond_fc = nn.Linear(in_dim, out_dim)
            setattr(self, f'fc_bond_{i + 1}', bond_fc)
            self.fc_bond_layers.append(bond_fc)

        
        self.attach_predictor = nn.Sequential(
                                nn.Linear(2*dec_hidden_dim, dec_hidden_dim),
                                nn.ReLU(),
                                nn.Linear(dec_hidden_dim, 1)
                                )

    def _forward_atom_features(self, atom_feats):
        for layer in self.fc_atom_layers[:-1]:
            atom_feats = torch.relu(layer(atom_feats))
            atom_feats = self.dropout(atom_feats)
        return atom_feats
    
    def _forward_bond_features_sparse(self, atom_feats, bond_feats, edge_idx):
        atom_feats = torch.tanh(self.fc_atom_proj(atom_feats))
        bond_feats = torch.tanh(self.fc_bond_proj(bond_feats))

        x_sum = atom_feats[edge_idx[:,0]] + atom_feats[edge_idx[:,1]]
        bond_actions_feat = torch.cat([x_sum, bond_feats], dim=-1)


        for bond_layer in self.fc_bond_layers[:-1]:
            bond_actions_feat = torch.relu(bond_layer(bond_actions_feat))
            bond_actions_feat = self.dropout(bond_actions_feat)

        return bond_actions_feat


    def forward_embedding_sparse(self, x: dict) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        atom_feats = x['atom_feats']
        bond_feats = x['bond_feats']
        edge_idx = x['edge_idx']
        graph_id = x['graph_id']
        atom_mask = x["atom_mask"]
        # sub_graph_ids = x['sub_graph_ids']
        prev_atom_feats = atom_feats
        # prev_bond_feats = bond_feats

        for i, conv in enumerate(self.conv_layers):
            residual = self.residual and i % 2 == 1
            atom_feats, bond_feats = conv.forward(atom_feats, bond_feats, edge_idx, graph_id,
                              apply_activation=not residual)

            synthon_feats, synthon_bond_feats = conv.forward(atom_feats, bond_feats[x["edge_idx_remove_supernode"]], edge_idx[x["edge_idx_remove_supernode"]], graph_id,
                    apply_activation=not residual)

            atom_feats = self.dropout(atom_feats + synthon_feats)

            if residual:
                atom_feats = torch.relu(atom_feats + prev_atom_feats)
                # bond_feats = torch.relu(bond_feats + prev_bond_feats)
                prev_atom_feats = atom_feats
                # prev_bond_feats = bond_feats


        atom_feats = atom_mask.view(-1,1) * atom_feats
        node_state = atom_feats

        # calculate final features for atom and bond actions
        atom_actions_feat = self._forward_atom_features(atom_feats)
        bond_actions_feat = self._forward_bond_features_sparse(atom_feats, bond_feats, edge_idx)

        return node_state, atom_actions_feat, bond_actions_feat
  
    
    def atom_num_predictor(self, node_state, graph_id):

        hidden_states = scatter_sum(node_state, graph_id, dim=0)
        hidden_states = hidden_states[hidden_states.sum(1) != 0]
        return self.atom_num_layer(hidden_states)

    def forward(self, x: dict):
        node_state, atom_actions_feat, bond_actions_feat = self.forward_embedding_sparse(x)


        atom_actions_feat = self.fc_atom_layers[-1](atom_actions_feat)  
        bond_actions_feat = self.fc_bond_layers[-1](bond_actions_feat)  
        
        
        nodes_num = scatter_sum(torch.ones_like(x['graph_id']), x['graph_id'])
        atom_feat_indices = torch.arange(atom_actions_feat.shape[-1], device=device)
        node_indices = torch.tensor(sum([list(range(node_num)) for node_num in nodes_num], [])).to(device)
        edge_graph_id = x['graph_id'][x['edge_idx'][:,0]]
        
        shift = torch.cumsum(torch.cat([torch.zeros([1], device=nodes_num.device).long(), nodes_num]), dim=-1)
        edge_idx = x['edge_idx'] - shift[x['graph_id'][x['edge_idx'][:,0]]].view(-1,1).expand_as(x['edge_idx'])
        bond_feat_indices = torch.arange(bond_actions_feat.shape[-1], device=device)
        
        # bond_idx (batch_id, row, col, bond_action_id) and bond_score,  supernode, self-loop and add_bond_action have been removed by bond_action_mask
        bond_batch_id = edge_graph_id.unsqueeze(1).repeat(1, bond_actions_feat.shape[-1]).view(-1)
        bond_row_idx = edge_idx[:, 0].unsqueeze(1).repeat(1, bond_actions_feat.shape[-1]).view(-1)
        bond_col_idx = edge_idx[:, 1].unsqueeze(1).repeat(1, bond_actions_feat.shape[-1]).view(-1)
        bond_feat_idx = bond_feat_indices.repeat(bond_actions_feat.shape[0])
        bond_action_type = torch.ones_like(bond_feat_idx)
        bond_mask = x['bond_action_mask'].clone()
        bond_mask[:,-1] = 0
        
        
        # atom_idx (batch_id, row, col, atom_action_id) and atom_score
        atom_batch_id = x['graph_id'].unsqueeze(1).repeat(1, atom_actions_feat.shape[-1]).view(-1)
        atom_row_idx = torch.ones(len(atom_actions_feat.view(-1))).cuda() * max(nodes_num)
        atom_col_idx = node_indices.unsqueeze(1).repeat(1, atom_actions_feat.shape[-1]).view(-1)
        atom_feat_idx = atom_feat_indices.repeat(atom_actions_feat.shape[0])
        atom_action_type = torch.ones_like(atom_feat_idx)*0
        
        
        # new bond
        attach_flag = False
        attach_source_am = torch.tensor([one if one is not None else -1 for one in x['attach_source']], device=bond_mask.device)
        attach_target_am = torch.tensor([one if one is not None else -1 for one in x['attach_target']], device=bond_mask.device)
        if (attach_source_am > -1).any():
            graph_id = x['graph_id'].unique()
            nodes_num = nodes_num[nodes_num>0]
            
            attach_mask = attach_source_am>-1
            attach_batch_id = graph_id[attach_mask].repeat_interleave(nodes_num[attach_mask])
            attach_row_idx = torch.cat([torch.arange(N) for N in nodes_num[attach_mask]]).to(bond_mask.device)
            attach_col_idx = attach_source_am[attach_mask].repeat_interleave(nodes_num[attach_mask])
            attach_feat_idx = torch.ones_like(attach_col_idx)*(bond_actions_feat.shape[1]-1)
            attach_action_type = torch.zeros_like(attach_feat_idx)*2
            
            local_shift = torch.tensor([0]+nodes_num[attach_mask].cumsum(dim=0).tolist(), device=bond_mask.device)
            attach_target = torch.zeros_like(attach_row_idx)
            attach_target[attach_target_am[attach_mask]+local_shift[:attach_mask.sum()]] = 1
            
            attach_target_am[attach_mask]
            
            attach_feat = torch.cat([
                node_state[attach_row_idx+shift[attach_batch_id]],
                node_state[attach_col_idx+shift[attach_batch_id]]], dim=1)
            
            attach_actions_feat = self.attach_predictor(attach_feat)
            # remove self-loop and larger indexes
            attach_mask = (attach_row_idx<attach_col_idx).float()
            
            attach_flag = True
            
            
            

        
        
        scores = {"atom_idx": 
                        torch.stack([atom_batch_id, 
                                     atom_row_idx, 
                                     atom_col_idx, 
                                     atom_feat_idx,
                                     atom_action_type]).long(),
                   "atom_action":atom_actions_feat.view(-1),
                   "atom_mask": x['atom_action_mask'].view(-1),
                   
                   "bond_idx":
                        torch.stack([bond_batch_id, 
                                     bond_row_idx, 
                                     bond_col_idx, 
                                     bond_feat_idx,
                                     bond_action_type]).long(),
                   "bond_action":bond_actions_feat.view(-1),
                   "bond_mask": x['bond_action_mask'].view(-1)}
        
        if x.get("atom_target") is not None:
            scores["atom_target"] = x['atom_target']
            scores["bond_target"] = x['bond_target']
            
        
        if attach_flag:
            scores["attach_idx"] = torch.stack([attach_batch_id, 
                                     attach_row_idx, 
                                     attach_col_idx, 
                                     attach_feat_idx,
                                     attach_action_type]).long()
            
            scores["attach_action"] = attach_actions_feat.view(-1)
            scores["attach_mask"] = attach_mask.view(-1)
            scores["attach_target"] = attach_target
        else:
            scores["attach_idx"] = torch.zeros(5,0, device=bond_mask.device).long()
            scores["attach_action"] = torch.zeros(0, device=bond_mask.device)
            scores["attach_mask"] = torch.zeros(0, device=bond_mask.device)
            scores["attach_target"] = torch.zeros(0, device=bond_mask.device)
        
        return node_state, scores
