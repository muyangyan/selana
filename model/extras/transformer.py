"""
FUTR Transformer class.

Copy-paste from github.com/facebookresearch/detr/blob/main/models/transformer.py with modifications.

"""

import torch
import numpy as np
from torch import nn, Tensor
import torch.nn.functional as F
from einops import repeat, rearrange
import copy
from typing import Optional, List
import pickle
from model.extras.mha import MultiheadAttention

from graph_modules.gsnn.gsnn import GSNN
from graph_modules.graph.graph import Graph
from graph_modules.gsnn.gsnn_forward import get_context_vectors
from graph_modules.gat.gatv2 import ModifiedGATv2
from graph_modules.gat.gat_forward import get_node_representations
from graph_modules.gat.video_enc import VideoEncoder
from model.extras.weight_matrix import KnowledgeWeightingModel


class Transformer(nn.Module):

    def __init__(self, args, d_model=512, nhead=8, num_encoder_layers=6,
                 num_decoder_layers=6, dim_feedforward=2048, dropout=0.1,
                 activation="relu", normalize_before=False,
                 return_intermediate_dec=False):
        super().__init__()

        self.args = args
        self.d_head = d_head = d_model // nhead

        if args.kg_attn == True: 
            self.knowledge_weighting_encoder_model = KnowledgeWeightingModel(args)
            self.knowledge_weighting_decoder_model = KnowledgeWeightingModel(args)

            encoder_layer = TransformerEncoderLayer(args, d_model, nhead, dim_feedforward,
                                                    dropout, activation, normalize_before,
                                                    self.knowledge_weighting_encoder_model)

            decoder_layer = TransformerDecoderLayer(args, d_model, nhead, dim_feedforward,
                                                    dropout, activation, normalize_before,
                                                    self.knowledge_weighting_decoder_model)

        else:
            encoder_layer = TransformerEncoderLayer(args, d_model, nhead, dim_feedforward,
                                                    dropout, activation, normalize_before)

            decoder_layer = TransformerDecoderLayer(args, d_model, nhead, dim_feedforward,
                                                dropout, activation, normalize_before)

        encoder_norm = nn.LayerNorm(d_model) if normalize_before else None
        self.encoder = TransformerEncoder(args, encoder_layer, num_encoder_layers, encoder_norm)

        decoder_norm = nn.LayerNorm(d_model)
        self.decoder = TransformerDecoder(args, decoder_layer, num_decoder_layers, decoder_norm,
                                          return_intermediate=return_intermediate_dec)

        self._reset_parameters()

        self.d_model = d_model
        self.nhead = nhead

        self.device = torch.device('cuda')

        if args.kg_attn == True:
            self.graph = Graph()
            self.graph = pickle.load(open('/home/sarthak/code/FUTR/graph_kitchen.pkl', 'rb'))
            self.graph.getGlobalAdjacencyMat()

            if args.use_gsnn:
                self.gsnn_net = GSNN(args)
                self.gsnn_net = self.gsnn_net.to(torch.device(self.device))

                if args.condition_propagation:
                    self.video_encoder = VideoEncoder(input_size=args.hidden_dim, hidden_size=args.hidden_dim//4, 
                                                num_layers=2, output_size=args.condition_propagation_dim, max_len=1512)
                    self.video_encoder = self.video_encoder.to(torch.device(self.device))
            
            else:
                self.gat = ModifiedGATv2(args, in_features=args.state_dim*2, n_hidden=args.state_dim, 
                                        n_heads=args.state_dim, dropout=args.encoder_dropout, 
                                        share_weights=args.encoder_share_weights)
                self.gat = self.gat.to(torch.device(self.device))

                self.video_encoder = VideoEncoder(input_size=args.hidden_dim, hidden_size=args.hidden_dim//4, 
                                                num_layers=2, output_size=args.condition_propagation_dim, max_len=1512)
                self.video_encoder = self.video_encoder.to(torch.device(self.device))

    def _reset_parameters(self):
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)

    def forward(self, src, tgt, mask, tgt_mask, detections, target_nodes, tgt_key_padding_mask, query_embed, pos_embed, tgt_pos_embed, mode='train'):

        graph_output, importance_loss = None, None

        if self.args.kg_attn == True:

            conditioning_input = None
            if self.args.condition_propagation: 
                conditioning_input = self.video_encoder(src.transpose(0, 1))
        
            if self.args.use_gsnn:
                importance_loss, context_vectors = get_context_vectors(self.args, self.gsnn_net, self.graph, detections, target_nodes,
                                                                            conditioning_input=conditioning_input, mode=mode)
                graph_output = context_vectors

            else:
                conditioning_input = self.video_encoder(src.transpose(0, 1))

                node_representations = get_node_representations(self.args, self.graph, self.gat, device=self.device,
                                                                    conditioning_input=conditioning_input)
                graph_output = node_representations    

        memory = self.encoder(src, graph_output, src_key_padding_mask=mask, pos=pos_embed)
        hs = self.decoder(tgt, memory, graph_output, tgt_mask=tgt_mask, memory_key_padding_mask=mask, tgt_key_padding_mask=tgt_key_padding_mask,
                          pos=pos_embed, query_pos=query_embed, tgt_pos=tgt_pos_embed)

        return memory, hs, importance_loss

class TransformerEncoder(nn.Module) :

    def __init__(self, args, encoder_layer, num_layers, norm=None) :
        super().__init__()
        self.layers = _get_clones(encoder_layer, num_layers)
        self.num_layers = num_layers
        self.norm = norm

    def forward(self, src, graph_output,
                mask: Optional[Tensor] = None,
                src_key_padding_mask: Optional[Tensor] = None,
                pos: Optional[Tensor] = None):
        output = src

        for layer in self.layers:
            output = layer(output, graph_output, src_mask=mask,
                           src_key_padding_mask=src_key_padding_mask, pos=pos)

        if self.norm is not None:
            output = self.norm(output)

        return output

class TransformerDecoder(nn.Module):

    def __init__(self, args, decoder_layer, num_layers, norm=None, return_intermediate=False):
        super().__init__()
        self.layers = _get_clones(decoder_layer, num_layers)
        self.num_layers = num_layers
        self.norm = norm
        self.return_intermediate = return_intermediate

    def forward(self, tgt, memory, graph_output,
                tgt_mask: Optional[Tensor] = None,
                memory_mask: Optional[Tensor] = None,
                tgt_key_padding_mask: Optional[Tensor] = None,
                memory_key_padding_mask: Optional[Tensor] = None,
                pos: Optional[Tensor] = None,
                tgt_pos: Optional[Tensor] = None,
                query_pos: Optional[Tensor] = None):
        output = tgt

        intermediate = []

        for layer in self.layers:
            output = layer(output, memory, graph_output, tgt_mask=tgt_mask,
                           memory_mask=memory_mask,
                           tgt_key_padding_mask=tgt_key_padding_mask,
                           memory_key_padding_mask=memory_key_padding_mask,
                           pos=pos, query_pos=query_pos, tgt_pos=tgt_pos)
            if self.return_intermediate:
                intermediate.append(self.norm(output))

        if self.norm is not None:
            output = self.norm(output)
            if self.return_intermediate:
                intermediate.pop()
                intermediate.append(output)

        if self.return_intermediate:
            return torch.stack(intermediate)

        return output


class TransformerEncoderLayer(nn.Module):

    def __init__(self, args, d_model, nhead, dim_feedforward=2048, dropout=0.1,
                 activation="relu", normalize_before=False, knowledge_model=None):
        super().__init__()

        self.args = args

        # Implementation of Feedforward model
        self.linear1 = nn.Linear(d_model, dim_feedforward)
        self.dropout = nn.Dropout(dropout)
        self.linear2 = nn.Linear(dim_feedforward, d_model)

        self.norm1 = nn.LayerNorm(d_model)
        self.norm2 = nn.LayerNorm(d_model)
        self.dropout1 = nn.Dropout(dropout)
        self.dropout2 = nn.Dropout(dropout)

        self.activation = _get_activation_fn(activation)
        self.normalize_before = normalize_before
  
        if args.kg_attn == True:
            self.self_attn = MultiheadAttention(args, d_model, nhead, dropout=dropout, knowledge_model=knowledge_model)
        else:
            self.self_attn = nn.MultiheadAttention(d_model, nhead, dropout=dropout)

    def with_pos_embed(self, tensor, pos: Optional[Tensor]):
        return tensor if pos is None else tensor + pos

    def forward_post(self,
                     src,
                     graph_output,
                     src_mask: Optional[Tensor] = None,
                     src_key_padding_mask: Optional[Tensor] = None,
                     pos: Optional[Tensor] = None):

        q = k = v = self.with_pos_embed(src, pos)

        if self.args.kg_attn == True:
            src2 = self.self_attn(q, k, value=v, graph_output=graph_output, attn_mask=src_mask,
                              key_padding_mask=src_key_padding_mask)[0]
        else:
            src2 = self.self_attn(q, k, value=v, attn_mask=src_mask,
                              key_padding_mask=src_key_padding_mask)[0]
        
        
        src = src + self.dropout1(src2)
        src = self.norm1(src)
        src2 = self.linear2(self.dropout(self.activation(self.linear1(src))))
        src = src + self.dropout2(src2)
        src = self.norm2(src)
        return src

    def forward_pre(self, src, graph_output,
                    src_mask: Optional[Tensor] = None,
                    src_key_padding_mask: Optional[Tensor] = None,
                    pos: Optional[Tensor] = None):
        src2 = self.norm1(src)
        q = k = v = self.with_pos_embed(src2, pos)
        src2 = self.self_attn(q, k, value=v, graph_output=graph_output, attn_mask=src_mask,
                              key_padding_mask=src_key_padding_mask)[0]
        src = src + self.dropout1(src2)
        src2 = self.norm2(src)
        src2 = self.linear2(self.dropout(self.activation(self.linear1(src2))))
        src = src + self.dropout2(src2)
        return src

    def forward(self, src, graph_output,
                src_mask: Optional[Tensor] = None,
                src_key_padding_mask: Optional[Tensor] = None,
                pos: Optional[Tensor] = None):
        if self.normalize_before:
            return self.forward_pre(src, graph_output, src_mask, src_key_padding_mask, pos)
        return self.forward_post(src, graph_output, src_mask, src_key_padding_mask, pos)


class TransformerDecoderLayer(nn.Module):

    def __init__(self, args, d_model, nhead, dim_feedforward=2048, dropout=0.1,
                 activation="relu", normalize_before=False, knowledge_model=None):
        super().__init__()

        self.args = args

        # Implementation of Feedforward model
        self.linear1 = nn.Linear(d_model, dim_feedforward)
        self.dropout = nn.Dropout(dropout)
        self.linear2 = nn.Linear(dim_feedforward, d_model)

        self.norm1 = nn.LayerNorm(d_model)
        self.norm2 = nn.LayerNorm(d_model)
        self.norm3 = nn.LayerNorm(d_model)
        self.dropout1 = nn.Dropout(dropout)
        self.dropout2 = nn.Dropout(dropout)
        self.dropout3 = nn.Dropout(dropout)

        self.activation = _get_activation_fn(activation)
        self.normalize_before = normalize_before

        if args.kg_attn == True:
            self.self_attn = MultiheadAttention(args, d_model, nhead, dropout=dropout, knowledge_model=knowledge_model)
            self.multihead_attn = MultiheadAttention(args, d_model, nhead, dropout=dropout, knowledge_model=knowledge_model)
        else:
            self.self_attn = nn.MultiheadAttention(d_model, nhead, dropout=dropout)
            self.multihead_attn = nn.MultiheadAttention(d_model, nhead, dropout=dropout)

    def with_pos_embed(self, tensor, pos: Optional[Tensor]):
        return tensor if pos is None else tensor + pos

    def forward_post(self, tgt, memory, graph_output,
                     tgt_mask: Optional[Tensor] = None,
                     memory_mask: Optional[Tensor] = None,
                     tgt_key_padding_mask: Optional[Tensor] = None,
                     memory_key_padding_mask: Optional[Tensor] = None,
                     pos: Optional[Tensor] = None,
                     query_pos: Optional[Tensor] = None,
                     tgt_pos: Optional[Tensor] =None):

        q = k = v = self.with_pos_embed(tgt, query_pos)
            
        if self.args.kg_attn == True:
            tgt2 = self.self_attn(q, k, value=v, graph_output=graph_output, attn_mask=tgt_mask,
                              key_padding_mask=tgt_key_padding_mask)[0]
        else:
            tgt2 = self.self_attn(q, k, value=v, attn_mask=tgt_mask,
                              key_padding_mask=tgt_key_padding_mask)[0]

        tgt = tgt + self.dropout1(tgt2)
        tgt = self.norm1(tgt)
            
        if self.args.kg_attn == True:
            tgt2 = self.multihead_attn(query=self.with_pos_embed(tgt, query_pos),
                                   key=self.with_pos_embed(memory, pos),
                                   value=self.with_pos_embed(memory, pos),
                                   graph_output=graph_output,
                                   attn_mask=memory_mask,
                                   key_padding_mask=memory_key_padding_mask)[0]
        else:
            tgt2 = self.multihead_attn(query=self.with_pos_embed(tgt, query_pos),
                                   key=self.with_pos_embed(memory, pos),
                                   value=self.with_pos_embed(memory, pos),
                                   attn_mask=memory_mask,
                                   key_padding_mask=memory_key_padding_mask)[0]

        tgt = tgt + self.dropout2(tgt2)
        tgt = self.norm2(tgt)
        tgt2 = self.linear2(self.dropout(self.activation(self.linear1(tgt))))
        tgt = tgt + self.dropout3(tgt2)
        tgt = self.norm3(tgt)
        return tgt

    def forward_pre(self, tgt, memory, graph_output,
                    tgt_mask: Optional[Tensor] = None,
                    memory_mask: Optional[Tensor] = None,
                    tgt_key_padding_mask: Optional[Tensor] = None,
                    memory_key_padding_mask: Optional[Tensor] = None,
                    pos: Optional[Tensor] = None,
                    query_pos: Optional[Tensor] = None,
                    tgt_pos: Optional[Tensor] = None):
        tgt2 = self.norm1(tgt)
        q = k = v = self.with_pos_embed(tgt2, query_pos)
        
        if self.args.kg_attn == True:
            tgt2 = self.self_attn(q, k, value=v, graph_output=graph_output,
                                attn_mask=tgt_mask,
                                key_padding_mask=tgt_key_padding_mask)[0]
        else:
            tgt2 = self.self_attn(q, k, value=v, attn_mask=tgt_mask,
                              key_padding_mask=tgt_key_padding_mask)[0]

        tgt = tgt + self.dropout1(tgt2)
        tgt2 = self.norm2(tgt)
        
        if self.args.kg_attn == True:
            tgt2 = self.multihead_attn(query=self.with_pos_embed(tgt2, query_pos),
                                   key=self.with_pos_embed(memory, pos),
                                   value=memory, graph_output=graph_output, 
                                   attn_mask=memory_mask,
                                   key_padding_mask=memory_key_padding_mask)[0]
        else:
            tgt2 = self.multihead_attn(query=self.with_pos_embed(tgt2, query_pos),
                                   key=self.with_pos_embed(memory, pos),
                                   value=memory,  
                                   attn_mask=memory_mask,
                                   key_padding_mask=memory_key_padding_mask)[0]
        
        tgt = tgt + self.dropout2(tgt2)
        tgt2 = self.norm3(tgt)
        tgt2 = self.linear2(self.dropout(self.activation(self.linear1(tgt2))))
        tgt = tgt + self.dropout3(tgt2)
        return tgt

    def forward(self, tgt, memory, graph_output,
                tgt_mask: Optional[Tensor] = None,
                memory_mask: Optional[Tensor] = None,
                tgt_key_padding_mask: Optional[Tensor] = None,
                memory_key_padding_mask: Optional[Tensor] = None,
                pos: Optional[Tensor] = None,
                tgt_pos: Optional[Tensor] = None,
                query_pos: Optional[Tensor] = None):
        if self.normalize_before:
            return self.forward_pre(tgt, memory, graph_output, tgt_mask, memory_mask,
                                    tgt_key_padding_mask, memory_key_padding_mask, pos, query_pos)
        return self.forward_post(tgt, memory, graph_output, tgt_mask, memory_mask,
                                 tgt_key_padding_mask, memory_key_padding_mask, pos, query_pos)

def _get_clones(module, N):
    return nn.ModuleList([copy.deepcopy(module) for i in range(N)])

def _get_activation_fn(activation):
    """Return an activation function given a string"""
    if activation == "relu":
        return F.relu
    if activation == "gelu":
        return F.gelu
    if activation == "glu":
        return F.glu
    raise RuntimeError(F"activation should be relu/gelu, not {activation}.")
