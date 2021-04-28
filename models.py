from re import S
from numpy.ma import count
import torch
import torch.nn as nn
import numpy as np
import pandas as pd
import time
import torch.nn.functional as F
import sentencepiece as spm
import pairing
import utils
from tqdm import tqdm
from torch.nn.modules.distance import CosineSimilarity
from torch.nn.utils.rnn import pad_packed_sequence as unpack
from torch.nn.utils.rnn import pack_padded_sequence as pack
from evaluate import evaluate
from torch import optim
from compute_correlations import test_correlation

def load_model(data, args):
    model = torch.load(args.load_file)

    state_dict = model['state_dict']
    model_args = model['args']
    vocab = model['vocab']
    vocab_fr = model['vocab_fr']
    optimizer = model['optimizer']
    epoch = model['epoch']
    if hasattr(args, "outfile"):
        model_args.outfile = args.outfile
    if hasattr(args, "epochs"):
        model_args.epochs = args.epochs

    if model_args.model == "avg":
        model = Averaging(data, model_args, vocab, vocab_fr)
    elif model_args.model == "lstm":
        model = LSTM(data, model_args, vocab, vocab_fr)

    model.load_state_dict(state_dict)
    model.optimizer.load_state_dict(optimizer)

    return model, epoch

class ParaModel(nn.Module):

    def __init__(self, data, args, vocab, vocab_fr):
        super(ParaModel, self).__init__()

        self.data = data
        self.args = args
        self.gpu = args.gpu

        self.vocab = vocab
        self.vocab_fr = vocab_fr
        self.ngrams = args.ngrams

        self.delta = args.delta
        self.pool = args.pool

        self.dropout = args.dropout
        self.share_encoder = args.share_encoder
        self.share_vocab = args.share_vocab
        self.scramble_rate = args.scramble_rate
        self.zero_unk = args.zero_unk

        self.batchsize = args.batchsize
        self.max_megabatch_size = args.megabatch_size
        self.curr_megabatch_size = 1
        self.megabatch = []
        self.megabatch_anneal = args.megabatch_anneal
        self.increment = False

        self.sim_loss = nn.MarginRankingLoss(margin=self.delta)
        # self.cosine = CosineSimilarity()

        if hasattr(args, "load_emb") and args.load_emb:
            assert self.vocab_fr is None
            model = torch.load(args.load_emb)
            state_dict = model['state_dict']
            model_args = model['args']
            assert model_args.model == "avg"
            self.vocab = model['vocab']
            self.vocab_fr = model['vocab_fr']
            self.embedding = nn.Embedding(len(self.vocab), self.args.dim)
            self.embedding.load_state_dict({"weight": state_dict["embedding.weight"]})
            del model
        else:
            self.embedding = nn.Embedding(len(self.vocab), self.args.dim)
        if self.vocab_fr is not None:
            self.embedding_fr = nn.Embedding(len(self.vocab_fr), self.args.dim)
        # self.attn = nn.Parameter(torch.zeros(self.args.dim))
        # nn.init.normal_(self.attn)
        # self.embedding.requires_grad_(False)

        d = self.args.dim
        self.attn_forward = nn.Sequential(
            nn.Linear(d, d),
            nn.GELU(),
            # nn.Dropout(p=0.3),
            nn.Dropout(p=0.1),
            nn.Linear(d, d),
            nn.LayerNorm(d)
        )
        self.compare_forward = nn.Sequential(
            nn.Linear(d * 2, d),
            nn.GELU(),
            nn.Dropout(p=0.1),
            nn.Linear(d, d),
            nn.LayerNorm(d)
        )
        self.aggregate_forward = nn.Sequential(
            nn.Linear(d * 2, d),
            nn.GELU(),
            nn.Dropout(p=0.1),
            nn.Linear(d, d),
            nn.GELU(),
            nn.Dropout(p=0.1),
            nn.Linear(d, 1),
            nn.Sigmoid()
        )

        self.sp = None
        if args.sp_model:
            self.sp = spm.SentencePieceProcessor()
            self.sp.Load(args.sp_model)

    def save_params(self, epoch):
        torch.save({'state_dict': self.state_dict(),
                'vocab': self.vocab,
                'vocab_fr': self.vocab_fr,
                'args': self.args,
                'optimizer': self.optimizer.state_dict(),
                'epoch': epoch}, "{0}_{1}.pt".format(self.args.outfile, epoch))

    def torchify_batch(self, batch):
        max_len = 0
        for i in batch:
            if len(i.embeddings) > max_len:
                max_len = len(i.embeddings)

        batch_len = len(batch)

        np_sents = np.zeros((batch_len, max_len), dtype='int32')
        np_lens = np.zeros((batch_len,), dtype='int32')

        for i, ex in enumerate(batch):
            np_sents[i, :len(ex.embeddings)] = ex.embeddings
            np_lens[i] = len(ex.embeddings)

        idxs, lengths = torch.from_numpy(np_sents).long(), \
                               torch.from_numpy(np_lens).float().long()

        if self.gpu:
            idxs = idxs.cuda()
            lengths = lengths.cuda()
    
        return idxs, lengths

    def loss_function(self, g1, g2, p1, p2):
        g1g2 = self.cosine(g1, g2)
        g1p1 = self.cosine(g1, p1)
        g2p2 = self.cosine(g2, p2)

        ones = torch.ones(g1g2.size()[0])
        if self.gpu:
            ones = ones.cuda()

        loss = self.sim_loss(g1g2, g1p1, ones) + self.sim_loss(g1g2, g2p2, ones)

        return loss

    def scoring_function(self, g_idxs1, g_lengths1, g_idxs2, g_lengths2, fr0=0, fr1=0):
        g1 = self.encode(g_idxs1, g_lengths1, fr=fr0)
        g2 = self.encode(g_idxs2, g_lengths2, fr=fr1)
        return self.cosine(g1, g2)

    def cosine(self, tup1, tup2):
        v1, mask1 = tup1
        v2, mask2 = tup2
        # match matrix similar to attention score: match_ij = cosine(v1_i, v2_j)
        # Shape: B x L1 x L2
        # av1 = self.attn_forward(v1)
        # av2 = self.attn_forward(v2)
        # match = av1 @ av2.transpose(1, 2)
        match = v1 @ v2.transpose(1, 2)
        # B x L1 x 1 @ B x 1 x L2 => B x L1 x L2
        match_mask = mask1.unsqueeze(dim=2) @ mask2.unsqueeze(dim=1)
        match[~match_mask.bool()] = -10000

        # # DecAttn
        # beta = F.softmax(match, dim=2) @ v2
        # alpha = F.softmax(match, dim=1).transpose(1, 2) @ v1
        # v1i = self.compare_forward(torch.cat([v1, beta], dim=2))
        # v2j = self.compare_forward(torch.cat([v2, alpha], dim=2))
        # v1i = v1i.sum(dim=1)
        # v2j = v2j.sum(dim=1)
        # return F.cosine_similarity(v1i, v2j)
        # # ret = self.aggregate_forward(torch.cat([v1i, v2j], dim=1))
        # # return ret.squeeze(dim=1)

        # Bi attention with -Max trick
        s1 = -torch.max(match, dim=2)[0] / 100
        s1[~mask1.bool()] = -10000
        attn1 = F.softmax(s1, dim=1)
        v1 = (v1 * attn1.unsqueeze(dim=2)).sum(dim=1)
        s2 = -torch.max(match, dim=1)[0] / 100
        s2[~mask2.bool()] = -10000
        attn2 = F.softmax(s2, dim=1)
        v2 = (v2 * attn2.unsqueeze(dim=2)).sum(dim=1)

        return F.cosine_similarity(v1, v2)

    def train_epochs(self, start_epoch=1):
        start_time = time.time()
        self.megabatch = []
        self.ep_loss = 0
        self.curr_idx = 0

        self.eval()
        evaluate(self, self.args)

        self.train()
        self.eval_csv(self.args)

        try:
            for ep in range(start_epoch, self.args.epochs+1):
                self.args.temperature = max(1, self.args.temperature * 0.5 ** (1 / 2))
                print("T = ", self.args.temperature)
                self.mb = utils.get_minibatches_idx(len(self.data), self.args.batchsize, shuffle=True)
                self.curr_idx = 0
                self.ep_loss = 0
                self.megabatch = []
                cost = 0
                counter = 0

                def tmp():
                    while True:
                        yield None
                tqdm_iter = tqdm(tmp())
                tqdm_iter_gen = tqdm_iter.__iter__()
                while (cost is not None):
                    _ = next(tqdm_iter_gen)
                    cost = pairing.compute_loss_one_batch(self)
                    if cost is None:
                        continue

                    if counter % 10 == 0:
                        tqdm_iter.set_description(f"batch loss = {cost:.3f}")
                    self.ep_loss += cost.item()
                    counter += 1

                    self.optimizer.zero_grad()
                    cost.backward()
                    torch.nn.utils.clip_grad_norm_(self.parameters, self.args.grad_clip)
                    # print(self.kernel_output.weight.grad, self.kernel_output.weight.grad.shape)
                    # print(self.mus.grad, self.mus.grad.shape)
                    # print(self.sigmas.grad, self.sigmas.grad.shape)
                    self.optimizer.step()
                    if counter % 200 == 0:
                        self.eval_csv(self.args)

                self.eval()
                evaluate(self, self.args)
                self.train()

                if self.args.save_every_epoch:
                    self.save_params(ep)

                print('Epoch {0}\tCost: '.format(ep), self.ep_loss / counter)
                self.eval_csv(self.args)

        except KeyboardInterrupt:
            print("Training Interrupted")

        end_time = time.time()
        print("Total Time:", (end_time - start_time))

    def eval_csv(self, args):
        """Evaluate and write results on the IdBench csv files
        
        args.small, args.medium, args.large
        """
        for csv_fname in [args.small, args.medium, args.large]:
            pairs = pd.read_csv(csv_fname, dtype=object)
            sim = []
            for var1, var2 in zip(pairs["id1"].tolist(), pairs["id2"].tolist()):
                if self.sp is not None:
                    var1_pieces = " ".join(self.sp.encode_as_pieces(utils.canonicalize(var1)))
                    var2_pieces = " ".join(self.sp.encode_as_pieces(utils.canonicalize(var2)))
                else:
                    var1_pieces = utils.canonicalize(var1)
                    var2_pieces = utils.canonicalize(var2)
                # print(var1_pieces, var2_pieces)
                wp1 = utils.Example(var1_pieces)
                wp2 = utils.Example(var2_pieces)
                if self.sp is not None:
                    wp1.populate_embeddings(self.vocab, self.zero_unk, 0)
                    wp2.populate_embeddings(self.vocab, self.zero_unk, 0)
                else:
                    wp1.populate_embeddings(self.vocab, self.zero_unk, self.ngrams)
                    wp2.populate_embeddings(self.vocab, self.zero_unk, self.ngrams)
                wx1, wl1 = self.torchify_batch([wp1])
                wx2, wl2 = self.torchify_batch([wp2])
                scores = self.scoring_function(wx1, wl1, wx2, wl2, fr0=False, fr1=False)
                # print(scores.item())
                sim.append(f"{scores.item():.4f}")
            pairs[self.args.name] = sim
            pairs.to_csv(csv_fname, index=False)

        test_correlation(args)

class Averaging(ParaModel):
    def __init__(self, data, args, vocab, vocab_fr):
        super(Averaging, self).__init__(data, args, vocab, vocab_fr)
        self.parameters = self.parameters()
        self.optimizer = optim.Adam(self.parameters, lr=self.args.lr)

        if args.gpu:
           self.cuda()

        print(self)
        
    def forward(self, curr_batch):
        g_idxs1 = curr_batch.g1
        g_lengths1 = curr_batch.g1_l

        g_idxs2 = curr_batch.g2
        g_lengths2 = curr_batch.g2_l

        p_idxs1 = curr_batch.p1
        p_lengths1 = curr_batch.p1_l

        p_idxs2 = curr_batch.p2
        p_lengths2 = curr_batch.p2_l

        g1 = self.encode(g_idxs1, g_lengths1)
        g2 = self.encode(g_idxs2, g_lengths2, fr=1)
        p1 = self.encode(p_idxs1, p_lengths1, fr=1)
        p2 = self.encode(p_idxs2, p_lengths2)

        return g1, g2, p1, p2

    def encode(self, idxs, lengths, fr=0):
        if fr and not self.share_vocab:
            word_embs = self.embedding_fr(idxs)
        else:
            word_embs = self.embedding(idxs)

        bs, max_len, _ = word_embs.shape
        mask = (torch.arange(max_len).cuda().expand(bs, max_len) < lengths.unsqueeze(1)).float()
        # s = (word_embs * self.attn).sum(dim=-1) / self.args.temperature
        # s[~mask] = -10000
        # a = F.softmax(s, dim=-1)
        # assert self.pool == "mean"
        # word_embs = (a.unsqueeze(dim=1) @ word_embs).squeeze(dim=1)
        # print(a)

        if self.dropout > 0:
            F.dropout(word_embs, training=self.training)

        # if self.pool == "max":
        #     word_embs = utils.max_pool(word_embs, lengths, self.args.gpu)
        # elif self.pool == "mean":
        #     word_embs = utils.mean_pool(word_embs, lengths, self.args.gpu)

        return word_embs, mask

class LSTM(ParaModel):
    def __init__(self, data, args, vocab, vocab_fr):
        super(LSTM, self).__init__(data, args, vocab, vocab_fr)

        self.hidden_dim = self.args.hidden_dim

        self.e_hidden_init = torch.zeros(2, 1, self.args.hidden_dim)
        self.e_cell_init = torch.zeros(2, 1, self.args.hidden_dim)

        if self.gpu:
            self.e_hidden_init = self.e_hidden_init.cuda()
            self.e_cell_init = self.e_cell_init.cuda()

        self.lstm = nn.LSTM(self.args.dim, self.hidden_dim, num_layers=1, bidirectional=True, batch_first=True)

        if not self.share_encoder:
            self.lstm_fr = nn.LSTM(self.args.dim, self.hidden_dim, num_layers=1,
                                       bidirectional=True, batch_first=True)

        self.parameters = self.parameters()
        self.optimizer = optim.Adam(filter(lambda p: p.requires_grad, self.parameters), self.args.lr)

        if self.gpu:
           self.cuda()

        print(self)

    def encode(self, inputs, lengths, fr=0):
        bsz, max_len = inputs.size()
        e_hidden_init = self.e_hidden_init.expand(2, bsz, self.hidden_dim).contiguous()
        e_cell_init = self.e_cell_init.expand(2, bsz, self.hidden_dim).contiguous()
        lens, indices = torch.sort(lengths, 0, True)

        if fr and not self.share_vocab:
            in_embs = self.embedding_fr(inputs)
        else:
            in_embs = self.embedding(inputs)

        if fr and not self.share_encoder:
            if self.dropout > 0:
                F.dropout(in_embs, training=self.training)
            all_hids, (enc_last_hid, _) = self.lstm_fr(pack(in_embs[indices],
                                                        lens.tolist(), batch_first=True), (e_hidden_init, e_cell_init))
        else:
            if self.dropout > 0:
                F.dropout(in_embs, training=self.training)
            all_hids, (enc_last_hid, _) = self.lstm(pack(in_embs[indices],
                                                         lens.tolist(), batch_first=True), (e_hidden_init, e_cell_init))

        _, _indices = torch.sort(indices, 0)
        all_hids = unpack(all_hids, batch_first=True)[0][_indices]

        # if self.pool == "max":
        #     embs = utils.max_pool(all_hids, lengths, self.gpu)
        # elif self.pool == "mean":
        #     embs = utils.mean_pool(all_hids, lengths, self.gpu)
        bs, max_len, _ = all_hids.shape
        mask = (torch.arange(max_len).cuda().expand(bs, max_len) < lengths.unsqueeze(1)).float()
        return all_hids, mask

    def forward(self, curr_batch):
        g_idxs1 = curr_batch.g1
        g_lengths1 = curr_batch.g1_l

        g_idxs2 = curr_batch.g2
        g_lengths2 = curr_batch.g2_l

        p_idxs1 = curr_batch.p1
        p_lengths1 = curr_batch.p1_l

        p_idxs2 = curr_batch.p2
        p_lengths2 = curr_batch.p2_l

        g1 = self.encode(g_idxs1, g_lengths1)
        g2 = self.encode(g_idxs2, g_lengths2, fr=1)
        p1 = self.encode(p_idxs1, p_lengths1, fr=1)
        p2 = self.encode(p_idxs2, p_lengths2)

        return g1, g2, p1, p2
