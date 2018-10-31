# Copyright 2017 Neural Networks and Deep Learning lab, MIPT
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import tensorflow as tf
import numpy as np

from copy import deepcopy
import shutil

from deeppavlov.core.common.registry import register
from deeppavlov.core.models.tf_model import TFModel
from deeppavlov.models.squad.utils_refactor import *
from deeppavlov.core.common.check_gpu import GPU_AVAILABLE
from deeppavlov.core.layers.tf_layers import variational_dropout
from deeppavlov.core.common.log import get_logger

logger = get_logger(__name__)

eps = 1e-06


@register('squad_model_ref')
class SquadModelRef(TFModel):
    def __init__(self, **kwargs):
        self.opt = deepcopy(kwargs)
        self.init_word_emb = self.opt['word_emb']
        self.init_char_emb = self.opt['char_emb']
        self.context_limit = self.opt['context_limit']
        self.question_limit = self.opt['question_limit']
        self.char_limit = self.opt['char_limit']
        self.char_hidden_size = self.opt['char_hidden_size']
        self.hidden_size = self.opt['encoder_hidden_size']
        self.attention_hidden_size = self.opt['attention_hidden_size']
        self.keep_prob = self.opt['keep_prob']
        self.input_keep_prob = self.opt.get('input_keep_prob', self.keep_prob)
        self.learning_rate = self.opt['learning_rate']
        self.min_learning_rate = self.opt['min_learning_rate']
        self.learning_rate_patience = self.opt['learning_rate_patience']
        self.learning_rate_decay_factor = self.opt['learning_rate_decay_factor', 2]
        self.grad_clip = self.opt['grad_clip']
        self.weight_decay = self.opt.get('weight_decay', 1.0)
        self.squad_loss_weight = self.opt.get('squad_loss_weight', 1.0)
        self.focal_loss_exp = self.opt.get('focal_loss_exp', 0.0)
        self.predict_ans = self.opt.get('predict_answer', True)
        self.noans_token = self.opt.get('noans_token', False)
        self.scorer = self.opt.get('scorer', False)
        self.use_features = self.opt.get('use_features', False)
        self.use_ner_features = self.opt.get('use_ner_features', False)
        self.features_dim = self.opt.get('features_dim', 2)
        self.ner_features_dim = self.opt.get('ner_features_dim', 8)
        self.ner_vocab_size = self.opt.get('ner_vocab_size', 20)
        self.use_elmo = self.opt.get('use_elmo', False)
        self.soft_labels = self.opt.get('soft_labels', False)
        self.true_label_weight = self.opt.get('true_label_weight', 0.7)
        self.use_gated_attention = self.opt.get('use_gated_attention', True)
        self.transform_char_emb = self.opt.get('transform_char_emb', 0)
        self.transform_word_emb = self.opt.get('transform_word_emb', 0)
        self.drop_diag_self_att = self.opt.get('drop_diag_self_att', False)
        self.use_birnn_after_coatt = self.opt.get('use_birnn_after_coatt', True)
        self.use_birnn_after_selfatt = self.opt.get('use_birnn_after_selfatt', True)
        self.use_highway_after_coatt = self.opt.get('use_highway_after_coatt', False)
        self.use_highway_after_selfatt = self.opt.get('use_highway_after_selfatt', False)
        self.use_transpose_att = self.opt.get('use_transpose_att', False)
        self.hops_keep_prob = self.opt.get('hops_keep_prob', 0.6)
        self.number_of_answer_hops = self.opt.get('number_of_answer_hops', 1)
        self.number_of_hops = self.opt.get('number_of_hops', 1)
        self.legacy = self.opt.get('legacy', False)  # support old checkpoints
        self.answer_cell_size = self.opt.get('answer_cell_size', 128)
        self.num_encoder_layers = self.opt.get('num_encoder_layers', 3)
        self.num_match_layers = self.opt.get('num_match_layers', 1)
        self.use_focal_loss = self.opt.get('use_focal_loss', False)
        self.share_layers = self.opt.get('share_layers', False)
        self.concat_bigru_outputs = self.opt.get('concat_bigru_outputs', True)
        self.shared_loss = self.opt.get('shared_loss', False)
        self.elmo_link = self.opt.get('elmo_link', 'https://tfhub.dev/google/elmo/2')
        self.use_soft_match_features = self.opt.get('use_soft_match_features', False)
        self.l2_norm = self.opt.get('l2_norm', None)
        self.concat_att_inputs = self.opt.get('concat_att_inputs', True)
        self.use_reattention = self.opt.get('use_reattention', False)
        self.answer_selector = self.opt.get('answer_selector', 'pointer_net')

        # TODO: add l2 norm to all dense layers and variables

        assert self.number_of_answer_hops > 0, "Number of answer hops is {}, " \
                                               "but should be > 0".format(self.number_of_answer_hops)

        assert self.number_of_hops > 0, "Number of hops is {}, " \
                                        "but should be > 0".format(self.number_of_hops)

        answer_selectors = ['pointer_net', 'mnemonic_reader', 'san']
        assert self.answer_selector in answer_selectors, "Answer selector should be on from: {}".format(
            ', '.join(answer_selectors))

        self.word_emb_dim = self.init_word_emb.shape[1]
        self.char_emb_dim = self.init_char_emb.shape[1]

        self.last_impatience = 0
        self.lr_impatience = 0

        # TODO: model is saved and loaded only with trainable variables (not saveable!)
        if GPU_AVAILABLE:
            self.GRU = CudnnGRU if not self.legacy else CudnnGRULegacy
        else:
            raise RuntimeError('SquadModel requires GPU')

        self.sess_config = tf.ConfigProto(allow_soft_placement=True)
        self.sess_config.gpu_options.allow_growth = True
        self.sess = tf.Session(config=self.sess_config)

        self._init_graph()

        self._init_optimizer()

        if self.weight_decay < 1.0:
            self._init_ema()

        self.sess.run(tf.global_variables_initializer())

        super().__init__(**kwargs)

        self.tmp_model_path = self.load_path.parent / 'tmp_dir' / self.load_path.name

        # Try to load the model (if there are some model files the model will be loaded from them)
        if self.load_path is not None:
            self.load()
            if self.weight_decay < 1.0:
                self._assign_ema_weights()

    def _init_graph(self):
        self._init_placeholders()

        self._prepare_placeholders_usage()

        c_emb, q_emb = self._build_token_embeddings()

        c_emb, q_emb = self._add_token_features(c_emb, q_emb)

        if self.transform_word_emb != 0:
            c_emb = transform_layer(c_emb, self.transform_word_emb, scope='transform_word_emb')
            q_emb = transform_layer(q_emb, self.transform_word_emb, scope='transform_word_emb', reuse=True)

        if self.use_elmo:
            # TODO: add elmo after encoding layer?
            import tensorflow_hub as tfhub
            elmo_module = tfhub.Module(self.elmo_link)
            c_elmo = elmo_embedding_layer(self.c_str, elmo_module)
            q_elmo = elmo_embedding_layer(self.q_str, elmo_module)

            c_emb = tf.concat([c_emb, c_elmo], axis=2)
            q_emb = tf.concat([q_emb, q_elmo], axis=2)

        with tf.variable_scope('encoding'):
            rnn = self.GRU(num_layers=self.num_encoder_layers, num_units=self.hidden_size, batch_size=self.bs,
                           input_size=c_emb.get_shape().as_list()[-1],
                           keep_prob=self.input_keep_prob_ph, share_layers=self.share_layers)
            c = rnn(c_emb, seq_len=self.c_len, concat_layers=self.concat_bigru_outputs)
            q = rnn(q_emb, seq_len=self.q_len, concat_layers=self.concat_bigru_outputs)

        context_representations = [c]
        E = None  # check reinforced mnemonic reader paper for more info about E, B and re-attention
        B = None
        for i in range(self.number_of_hops):
            with tf.variable_scope('co-attention_{}'.format(i)):
                if not self.use_reattention:
                    qc_att = dot_attention(context_representations[-1], q, mask=self.q_mask,
                                           att_size=self.attention_hidden_size, keep_prob=self.keep_prob_ph,
                                           use_gate=self.use_gated_attention, use_transpose_att=self.use_transpose_att,
                                           concat_inputs=self.concat_att_inputs)
                else:
                    qc_att, E = dot_reattention(context_representations[-1], q, memory_mask=self.q_mask,
                                                inputs_mask=self.c_mask, att_size=self.attention_hidden_size,
                                                E=E, B=B,
                                                keep_prob=self.keep_prob_ph, concat_inputs=self.concat_att_inputs)

                if self.use_highway_after_coatt:
                    # qc_att = tf.layers.batch_normalization(qc_att, training=self.is_train_ph)
                    qc_att = highway_layer(variational_dropout(context_representations[-1], keep_prob=self.keep_prob_ph),
                                           variational_dropout(qc_att, keep_prob=self.keep_prob_ph),
                                           use_combinations=True, regularizer=tf.nn.l2_loss)

                if self.use_birnn_after_coatt:
                    rnn = self.GRU(num_layers=self.num_match_layers, num_units=self.hidden_size, batch_size=self.bs,
                                   input_size=qc_att.get_shape().as_list()[-1],
                                   keep_prob=self.keep_prob_ph, share_layers=self.share_layers)
                    qc_att = rnn(qc_att, seq_len=self.c_len, concat_layers=self.concat_bigru_outputs)

            with tf.variable_scope('self-attention_{}'.format(i)):
                if not self.use_reattention:
                    match = dot_attention(qc_att, qc_att, mask=self.c_mask, att_size=self.attention_hidden_size,
                                          keep_prob=self.keep_prob_ph, use_gate=self.use_gated_attention,
                                          drop_diag=self.drop_diag_self_att, use_transpose_att=False,
                                          concat_inputs=self.concat_att_inputs)
                else:
                    match, B = dot_reattention(qc_att, qc_att, memory_mask=self.c_mask,
                                               inputs_mask=self.c_mask, att_size=self.attention_hidden_size,
                                               E=B, B=B, drop_diag=self.drop_diag_self_att,
                                               keep_prob=self.keep_prob_ph, concat_inputs=self.concat_att_inputs)

                if self.use_highway_after_selfatt:
                    # Z
                    # match = tf.layers.batch_normalization(match, training=self.is_train_ph)
                    match = highway_layer(variational_dropout(qc_att, keep_prob=self.keep_prob_ph),
                                          variational_dropout(match, keep_prob=self.keep_prob_ph),
                                          use_combinations=True, regularizer=tf.nn.l2_loss)

                if self.use_birnn_after_selfatt:
                    # R
                    rnn = self.GRU(num_layers=self.num_match_layers, num_units=self.hidden_size, batch_size=self.bs,
                                   input_size=match.get_shape().as_list()[-1],
                                   keep_prob=self.keep_prob_ph, share_layers=self.share_layers)
                    match = rnn(match, seq_len=self.c_len, concat_layers=self.concat_bigru_outputs)

                context_representations.append(match)
        
        if self.number_of_hops == 1:
            final_context_repr = context_representations[-1]
        else:
            with tf.variable_scope('aggregation'):
                context_representations = tf.concat(context_representations, axis=-1)
                rnn = self.GRU(num_layers=self.num_match_layers, num_units=self.hidden_size, batch_size=self.bs,
                               input_size=context_representations.get_shape().as_list()[-1],
                               keep_prob=self.keep_prob_ph, share_layers=self.share_layers)
                final_context_repr = rnn(context_representations,
                                         seq_len=self.c_len,
                                         concat_layers=self.concat_bigru_outputs)

        if self.predict_ans:
            with tf.variable_scope('answer_selection'):

                if self.noans_token:
                    # create trainable no_ans token vector and add it to context at the first position
                    noans_token = tf.Variable(tf.random_uniform((final_context_repr.get_shape().as_list()[-1],),
                                                                -0.1, 0.1), tf.float32)
                    noans_token = tf.nn.dropout(noans_token, keep_prob=self.keep_prob_ph)
                    noans_token = tf.expand_dims(tf.tile(tf.expand_dims(noans_token, axis=0), [self.bs, 1]), axis=1)
                    final_context_repr = tf.concat([noans_token, final_context_repr], axis=1)
                    self.c_mask = tf.concat([tf.ones(shape=(self.bs, 1), dtype=tf.bool), self.c_mask], axis=1)

                if self.answer_selector == 'pointer_net':
                    logits_st, logits_end = pointer_net_answer_selection(q, final_context_repr, self.q_mask,
                                                                         self.c_mask,
                                                                         self.attention_hidden_size,
                                                                         keep_prob=self.keep_prob_ph)
                elif self.answer_selector == 'mnemonic_reader':
                    logits_st, logits_end = mnemonic_reader_answer_selection(q, final_context_repr, self.q_mask,
                                                                             self.c_mask,
                                                                             self.attention_hidden_size,
                                                                             keep_prob=self.keep_prob_ph)
                elif self.answer_selector == 'san':
                    # TODO check noans_token support
                    logits_st, logits_end = san_answer_selection(q, final_context_repr, self.q_mask, self.c_mask,
                                                                 self.number_of_answer_hops, self.attention_hidden_size,
                                                                 self.answer_cell_size, self.keep_prob_ph,
                                                                 self.hops_keep_prob_ph)
        if self.scorer:
            with tf.variable_scope('scorer'):
                start_att_weights = tf.expand_dims(tf.nn.softmax(logits_st, axis=-1), axis=-1)
                end_att_weights = tf.expand_dims(tf.nn.softmax(logits_end, axis=-1), axis=-1)
                # not really good idea to sum only start and end tokens
                # need some kind of sigmoid layer here
                start_att = tf.reduce_sum(start_att_weights * final_context_repr, axis=1)
                end_att = tf.reduce_sum(end_att_weights * final_context_repr, axis=1)
                c_att = simple_attention(final_context_repr, self.hidden_size, mask=self.c_mask,
                                         keep_prob=self.keep_prob_ph, scope='c_att')
                q_att = simple_attention(q, self.hidden_size, mask=self.q_mask,
                                         keep_prob=self.keep_prob_ph, scope='q_att')

                dense_input = tf.concat([start_att, end_att, c_att, q_att], -1)
                layer_1_logits = tf.nn.dropout(
                    tf.layers.dense(dense_input,
                                    units=self.hidden_size * 2,
                                    activation=tf.nn.relu,
                                    kernel_initializer=tf.contrib.layers.variance_scaling_initializer(),
                                    name='scorer_dense_1'),
                    keep_prob=self.keep_prob_ph)

                layer_2_logits = tf.nn.dropout(
                    tf.layers.dense(layer_1_logits,
                                    units=self.hidden_size,
                                    activation=tf.nn.relu,
                                    kernel_initializer=tf.contrib.layers.variance_scaling_initializer(),
                                    name='scorer_dense_2'),
                    keep_prob=self.keep_prob_ph)

                # if scorer_logits large - no ans, if zs small answer exist
                scorer_logits = tf.squeeze(tf.layers.dense(layer_2_logits, units=1, name='scorer_logits'), axis=-1)

        # make predictions
        with tf.variable_scope('predict'):
            if self.predict_ans and not self.shared_loss:
                outer_logits = tf.exp(tf.expand_dims(logits_st, axis=2) + tf.expand_dims(logits_end, axis=1))
                outer_logits = tf.matrix_band_part(outer_logits, 0, tf.cast(tf.minimum(15, self.c_maxlen), tf.int64))
                outer = tf.matmul(tf.expand_dims(tf.nn.softmax(logits_st), axis=2),
                                  tf.expand_dims(tf.nn.softmax(logits_end), axis=1))
                outer = tf.matrix_band_part(outer, 0, tf.cast(tf.minimum(15, self.c_maxlen), tf.int64))
                self.yp1 = tf.argmax(tf.reduce_max(outer, axis=2), axis=1)
                self.yp2 = tf.argmax(tf.reduce_max(outer, axis=1), axis=1)
                self.yp_prob = tf.reduce_max(tf.reduce_max(outer, axis=2), axis=1)
                self.yp_logits = tf.reduce_max(tf.reduce_max(outer_logits, axis=2), axis=1)
                if self.noans_token:
                    self.noans_prob = 1 - tf.nn.softmax(logits_st)[:, 0] * tf.nn.softmax(logits_end)[:, 0]

            if self.predict_ans and self.shared_loss and self.scorer:
                # we want to predict answer and to use shared loss
                logits = tf.reshape(tf.expand_dims(logits_st, 1) + tf.expand_dims(logits_end, 2), (self.bs, -1))
                all_logits = tf.concat([tf.expand_dims(scorer_logits, axis=-1), logits], axis=-1)

                labels = tf.cast(
                    tf.reshape(tf.logical_and(tf.expand_dims(tf.cast(self.y1, tf.bool), 1),
                                              tf.expand_dims(tf.cast(self.y2, tf.bool), 2)), (self.bs, -1)), tf.float32)
                # self.y 1 if answer is present, 0 otherwise
                all_labels = tf.concat([tf.expand_dims(1-tf.cast(self.y, tf.float32), axis=-1), labels], axis=-1)
                all_sum = tf.reduce_logsumexp(all_logits, axis=-1)

                outer_logits = tf.exp(tf.expand_dims(logits_st, axis=2) + tf.expand_dims(logits_end, axis=1))
                outer_logits = tf.matrix_band_part(outer_logits, 0, tf.cast(tf.minimum(15, self.c_maxlen), tf.int64))
                self.yp1 = tf.argmax(tf.reduce_max(outer_logits, axis=2), axis=1)
                self.yp2 = tf.argmax(tf.reduce_max(outer_logits, axis=1), axis=1)
                self.yp_logits = tf.reduce_max(tf.reduce_max(outer_logits, axis=2), axis=1)
                self.yp_prob = self.yp_logits / tf.exp(all_sum)

            if self.scorer:
                self.noans_prob = 1 - tf.nn.sigmoid(scorer_logits)

            # compute loss
            if self.predict_ans and not self.shared_loss:
                # SQuAD loss: sum of start and end positions predictions
                loss_start = tf.nn.softmax_cross_entropy_with_logits(logits=logits_st, labels=self.y1)
                loss_end = tf.nn.softmax_cross_entropy_with_logits(logits=logits_end, labels=self.y2)
                squad_loss = loss_start + loss_end

            if self.scorer and not self.shared_loss:
                # scorer loss
                """
                if self.use_focal_loss:
                    # focal loss
                    # check bug?
                    scorer_loss = tf.pow(1 - yt_prob, self.focal_loss_exp) * \
                                  tf.nn.softmax_cross_entropy_with_logits(logits=scorer_logits, labels=self.y_ohe)
                else:
                """
                scorer_loss = tf.nn.sigmoid_cross_entropy_with_logits(logits=scorer_logits,
                                                                      labels=1-tf.cast(self.y, tf.float32))

                if self.predict_ans and not self.noans_token:
                    # skip examples without answer when calculate squad_loss
                    squad_loss = tf.boolean_mask(squad_loss, self.y)

            if self.predict_ans and self.shared_loss and self.scorer:
                correct_sum = tf.reduce_logsumexp(softmax_mask(all_logits, all_labels), axis=-1)
                shared_loss = -(correct_sum - all_sum)

            if self.predict_ans and self.scorer and not self.shared_loss:
                self.loss = self.squad_loss_weight * tf.reduce_mean(squad_loss) \
                            + (1 - self.squad_loss_weight) * tf.reduce_mean(scorer_loss)
            elif self.predict_ans and self.scorer and self.shared_loss:
                # TODO: use additional losses on qa task and noans task
                self.loss = tf.reduce_mean(shared_loss)
            elif self.scorer:
                self.loss = tf.reduce_mean(scorer_loss)
            else:
                self.loss = tf.reduce_mean(squad_loss)

            if self.l2_norm is not None:
                self.loss += self.l2_norm * tf.reduce_sum(tf.get_collection(tf.GraphKeys.REGULARIZATION_LOSSES))

    def _init_placeholders(self):
        self.c_ph = tf.placeholder(shape=(None, None), dtype=tf.int32, name='c_ph')
        self.cc_ph = tf.placeholder(shape=(None, None, self.char_limit), dtype=tf.int32, name='cc_ph')
        self.q_ph = tf.placeholder(shape=(None, None), dtype=tf.int32, name='q_ph')
        self.qc_ph = tf.placeholder(shape=(None, None, self.char_limit), dtype=tf.int32, name='qc_ph')

        if self.use_features:
            self.c_f_ph = tf.placeholder(shape=(None, None, self.features_dim), dtype=tf.float32, name='c_f_ph')
            self.q_f_ph = tf.placeholder(shape=(None, None, self.features_dim), dtype=tf.float32, name='q_f_ph')

        if self.use_ner_features:
            self.c_ner_ph = tf.placeholder(shape=(None, None), dtype=tf.int32, name='c_ner_ph')
            self.q_ner_ph = tf.placeholder(shape=(None, None), dtype=tf.int32, name='q_ner_ph')

        if self.use_elmo:
            self.c_str_ph = tf.placeholder(shape=(None, None), dtype=tf.string, name='c_str_ph')
            self.q_str_ph = tf.placeholder(shape=(None, None), dtype=tf.string, name='q_str_ph')

        if self.scorer:
            self.y_ph = tf.placeholder(shape=(None, ), dtype=tf.int32, name='y_ph')

        if self.predict_ans:
            self.y1_ph = tf.placeholder(shape=(None, ), dtype=tf.int32, name='y1_ph')
            self.y2_ph = tf.placeholder(shape=(None, ), dtype=tf.int32, name='y2_ph')

        self.lr_ph = tf.placeholder(dtype=tf.float32, shape=[], name='lr_ph')
        # add separate keep prob for inputs
        # to use higher keep prob in deeper layers
        self.keep_prob_ph = tf.placeholder_with_default(1.0, shape=[], name='keep_prob_ph')
        self.input_keep_prob_ph = tf.placeholder_with_default(1.0, shape=[], name='input_keep_prob_ph')
        self.hops_keep_prob_ph = tf.placeholder_with_default(1.0, shape=[], name='hops_keep_prob_ph')
        self.is_train_ph = tf.placeholder_with_default(False, shape=[], name='is_train_ph')

    def _prepare_placeholders_usage(self):
        self.c_mask = tf.cast(self.c_ph, tf.bool)
        self.q_mask = tf.cast(self.q_ph, tf.bool)
        self.c_len = tf.reduce_sum(tf.cast(self.c_mask, tf.int32), axis=1)
        self.q_len = tf.reduce_sum(tf.cast(self.q_mask, tf.int32), axis=1)

        self.bs = tf.shape(self.c_ph)[0]
        self.c_maxlen = tf.reduce_max(self.c_len)
        self.q_maxlen = tf.reduce_max(self.q_len)
        self.c = tf.slice(self.c_ph, [0, 0], [self.bs, self.c_maxlen])
        self.q = tf.slice(self.q_ph, [0, 0], [self.bs, self.q_maxlen])
        self.c_mask = tf.slice(self.c_mask, [0, 0], [self.bs, self.c_maxlen])
        self.q_mask = tf.slice(self.q_mask, [0, 0], [self.bs, self.q_maxlen])
        self.cc = tf.slice(self.cc_ph, [0, 0, 0], [self.bs, self.c_maxlen, self.char_limit])
        self.qc = tf.slice(self.qc_ph, [0, 0, 0], [self.bs, self.q_maxlen, self.char_limit])

        if self.use_features:
            self.c_f = tf.slice(self.c_f_ph, [0, 0, 0], [self.bs, self.c_maxlen, self.features_dim])
            self.q_f = tf.slice(self.q_f_ph, [0, 0, 0], [self.bs, self.q_maxlen, self.features_dim])

        if self.use_ner_features:
            self.c_ner = tf.slice(self.c_ner_ph, [0, 0], [self.bs, self.c_maxlen])
            self.q_ner = tf.slice(self.q_ner_ph, [0, 0], [self.bs, self.q_maxlen])

        if self.use_elmo:
            self.c_str = tf.slice(self.c_str_ph, [0, 0], [self.bs, self.c_maxlen])
            self.q_str = tf.slice(self.q_str_ph, [0, 0], [self.bs, self.q_maxlen])

        if self.noans_token:
            # we use additional 'no answer' token to allow model not to answer on question
            self.y1 = tf.one_hot(self.y1_ph, depth=self.context_limit + 1)
            self.y2 = tf.one_hot(self.y2_ph, depth=self.context_limit + 1)
            self.y1 = tf.slice(self.y1, [0, 0], [self.bs, self.c_maxlen + 1])
            self.y2 = tf.slice(self.y2, [0, 0], [self.bs, self.c_maxlen + 1])
        elif self.scorer:
            # we want to predict answer presence
            self.y = self.y_ph

        if self.predict_ans:
            self.y1 = tf.one_hot(self.y1_ph, depth=self.context_limit)
            self.y2 = tf.one_hot(self.y2_ph, depth=self.context_limit)
            self.y1 = tf.slice(self.y1, [0, 0], [self.bs, self.c_maxlen])
            self.y2 = tf.slice(self.y2, [0, 0], [self.bs, self.c_maxlen])

            if self.soft_labels:
                # do we still need this?
                center_weight = self.true_label_weight
                border_weight = (1 - self.true_label_weight) / 2
                smoothing_kernel_st = tf.constant([border_weight, center_weight, border_weight])
                smoothing_kernel_st = tf.reshape(smoothing_kernel_st, [3, 1, 1])
                # WARNING: smoothing_kernel_end with non-zero first value makes huge values in loss
                smoothing_kernel_end = tf.constant([0.0, center_weight + border_weight / 2, border_weight * 3 / 2])
                smoothing_kernel_end = tf.reshape(smoothing_kernel_end, [3, 1, 1])
                self.y1 = tf.expand_dims(self.y1, axis=-1)
                self.y2 = tf.expand_dims(self.y2, axis=-1)
                self.y1 = tf.squeeze(tf.nn.conv1d(self.y1, filters=smoothing_kernel_st, stride=1, padding='SAME'))
                self.y2 = tf.squeeze(tf.nn.conv1d(self.y2, filters=smoothing_kernel_end, stride=1, padding='SAME'))
                self.y1 = self.y1 / tf.expand_dims(tf.maximum(tf.reduce_sum(self.y1, axis=-1), 1e-3), axis=-1)
                self.y2 = self.y2 / tf.expand_dims(tf.maximum(tf.reduce_sum(self.y2, axis=-1), 1e-3), axis=-1)

    def _build_token_embeddings(self):
        with tf.variable_scope('emb'):
            with tf.variable_scope('char'):

                cc_emb = character_embedding_layer(self.cc, self.char_hidden_size, keep_prob=self.input_keep_prob_ph,
                                                   emb_mat_init=self.init_char_emb,
                                                   trainable_emb_mat=self.opt['train_char_emb'],
                                                   regularizer=tf.nn.l2_loss,
                                                   transform_char_emb=self.transform_char_emb)

                qc_emb = character_embedding_layer(self.qc, self.char_hidden_size, keep_prob=self.input_keep_prob_ph,
                                                   emb_mat_init=self.init_char_emb,
                                                   trainable_emb_mat=self.opt['train_char_emb'],
                                                   regularizer=tf.nn.l2_loss,
                                                   transform_char_emb=self.transform_char_emb, reuse=True)

            with tf.variable_scope('word'):
                c_emb = embedding_layer(self.c, self.init_word_emb, trainable=False)
                q_emb = embedding_layer(self.q, self.init_word_emb, trainable=False)

            # TODO: add highway layer?
            c_emb = tf.concat([c_emb, cc_emb], axis=2)
            q_emb = tf.concat([q_emb, qc_emb], axis=2)
        return c_emb, q_emb

    def _add_token_features(self, c_emb, q_emb):
        with tf.variable_scope('features'):
            if self.use_soft_match_features:
                c_soft_match = dot_attention(c_emb, q_emb, mask=self.q_mask,
                                             att_size=self.attention_hidden_size,
                                             keep_prob=self.keep_prob_ph, use_gate=False,
                                             use_transpose_att=False,
                                             concat_inputs=False,
                                             scope='c_word_attention')

                q_soft_match = dot_attention(q_emb, c_emb, mask=self.c_mask,
                                             att_size=self.attention_hidden_size,
                                             keep_prob=self.keep_prob_ph, use_gate=False,
                                             use_transpose_att=False,
                                             concat_inputs=False,
                                             scope='q_word_attention')

                c_emb = tf.concat([c_emb, c_soft_match], axis=2)
                q_emb = tf.concat([q_emb, q_soft_match], axis=2)

            if self.use_features:
                c_emb = tf.concat([c_emb, self.c_f], axis=2)
                q_emb = tf.concat([q_emb, self.q_f], axis=2)

            if self.use_ner_features:
                with tf.variable_scope('ner'):
                    c_ner_emb = embedding_layer(self.c_ner, vocab_size=self.ner_vocab_size,
                                                emb_dim=self.ner_features_dim, trainable=True, regularizer=tf.nn.l2_loss)
                    q_ner_emb = embedding_layer(self.q_ner, vocab_size=self.ner_vocab_size, emb_dim=self.ner_features_dim)

                    c_ner_emb = variational_dropout(c_ner_emb, keep_prob=self.input_keep_prob_ph)
                    q_ner_emb = variational_dropout(q_ner_emb, keep_prob=self.input_keep_prob_ph)

                c_emb = tf.concat([c_emb, c_ner_emb], axis=2)
                q_emb = tf.concat([q_emb, q_ner_emb], axis=2)
        return c_emb, q_emb

    def _init_optimizer(self):
        with tf.variable_scope('Optimizer'):
            self.global_step = tf.get_variable('global_step', shape=[], dtype=tf.int32,
                                               initializer=tf.constant_initializer(0), trainable=False)

            with tf.control_dependencies(tf.get_collection(tf.GraphKeys.UPDATE_OPS)):

                self.opt = tf.train.AdadeltaOptimizer(learning_rate=self.lr_ph, epsilon=1e-6)

                if self.predict_ans and self.scorer:
                    # TODO: check if it moved from contrib
                    self.opt = tf.contrib.opt.MultitaskOptimizerWrapper(self.opt)

                grads = self.opt.compute_gradients(self.loss)
                gradients, variables = zip(*grads)

                capped_grads = [tf.clip_by_norm(g, self.grad_clip) for g in gradients]

                self.train_op = self.opt.apply_gradients(zip(capped_grads, variables), global_step=self.global_step)

    def _init_ema(self):
        var_ema = tf.train.ExponentialMovingAverage(self.weight_decay)
        with tf.control_dependencies([self.train_op]):
            self.train_op = var_ema.apply(tf.trainable_variables())

        shadow_vars = []
        global_vars = []
        for var in tf.trainable_variables():
            v = var_ema.average(var)
            if v:
                shadow_vars.append(v)
                global_vars.append(var)

        self.assign_vars = []
        for g, v in zip(global_vars, shadow_vars):
            self.assign_vars.append(tf.assign(g, v))

    def _assign_ema_weights(self):
        logger.info('SQuAD model: Using EMA weights.')
        self.sess.run(self.assign_vars)

    def _build_feed_dict(self, c_tokens, c_chars, q_tokens, q_chars,
                         c_features=None, q_features=None, c_str=None, q_str=None, c_ner=None, q_ner=None,
                         y1=None, y2=None, y=None):

        if self.use_elmo:
            c_str = self._pad_strings(c_str, self.context_limit)
            q_str = self._pad_strings(q_str, self.question_limit)

        feed_dict = {
            self.c_ph: c_tokens,
            self.cc_ph: c_chars,
            self.q_ph: q_tokens,
            self.qc_ph: q_chars,
        }
        if self.use_features:
            assert c_features is not None and q_features is not None
            feed_dict.update({
                self.c_f_ph: c_features,
                self.q_f_ph: q_features,
            })
        if self.use_ner_features:
            assert c_ner is not None and q_ner is not None
            feed_dict.update({
                self.c_ner_ph: c_ner,
                self.q_ner_ph: q_ner,
            })
        if self.predict_ans and y1 is not None and y2 is not None:
            feed_dict.update({
                self.y1_ph: y1,
                self.y2_ph: y2,
                self.lr_ph: self.learning_rate,
                self.keep_prob_ph: self.keep_prob,
                self.input_keep_prob_ph: self.input_keep_prob,
                self.hops_keep_prob_ph: self.hops_keep_prob,
                self.is_train_ph: True,
            })
        if self.scorer and y is not None:
            feed_dict.update({
                self.y_ph: y,
                self.lr_ph: self.learning_rate,
                self.keep_prob_ph: self.keep_prob,
                self.input_keep_prob_ph: self.input_keep_prob,
                self.hops_keep_prob_ph: self.hops_keep_prob,
                self.is_train_ph: True,
            })
        if self.use_elmo:
            feed_dict.update({
                self.c_str_ph: c_str,
                self.q_str_ph: q_str
            })

        return feed_dict

    def train_on_batch(self, c_tokens, c_chars, q_tokens, q_chars,
                       c_features=None, q_features=None, c_str=None, q_str=None, c_ner=None, q_ner=None,
                       y1s=None, y2s=None):
        # TODO: filter examples in batches with answer position greater self.context_limit
        # select one answer from list of correct answers
        # y1s, y2s are start and end positions of answer
        y1s = list(map(lambda x: x[0], y1s))
        y2s = list(map(lambda x: x[0], y2s))
        ys = None
        if self.scorer:
            ys = [int(not (y1 == -1 and y2 == -1)) for y1, y2 in zip(y1s, y2s)]
        if self.noans_token and self.predict_ans:
            y1s_noans, y2s_noans = [], []
            for y1, y2 in zip(y1s, y2s):
                if y1 == -1 or y2 == -1:
                    y1s_noans.append(0)
                    y2s_noans.append(0)
                else:
                    y1s_noans.append(y1 + 1)
                    y2s_noans.append(y2 + 1)
            y1s, y2s = y1s_noans, y2s_noans
        feed_dict = self._build_feed_dict(c_tokens, c_chars, q_tokens, q_chars,
                                          c_features, q_features, c_str, q_str, c_ner, q_ner, y1s, y2s, ys)
        loss, _ = self.sess.run([self.loss, self.train_op], feed_dict=feed_dict)
        return loss

    def __call__(self, c_tokens, c_chars, q_tokens, q_chars,
                 c_features=None, q_features=None, c_str=None, q_str=None, c_ner=None, q_ner=None, *args, **kwargs):

        if any(np.sum(c_tokens, axis=-1) == 0) or any(np.sum(q_tokens, axis=-1) == 0):
            logger.info('SQuAD model: Warning! Empty question or context was found.')
            noanswers = -np.ones(shape=(c_tokens.shape[0]), dtype=np.int32)
            zero_probs = np.zeros(shape=(c_tokens.shape[0]), dtype=np.float32)
            if self.scorer and not self.predict_ans:
                return zero_probs
            if self.noans_token:
                return noanswers, noanswers, zero_probs, zero_probs
            return noanswers, noanswers, zero_probs, zero_probs

        feed_dict = self._build_feed_dict(c_tokens, c_chars, q_tokens, q_chars,
                                          c_features, q_features, c_str, q_str, c_ner, q_ner)
        if self.scorer and not self.predict_ans:
            score = self.sess.run(self.noans_prob, feed_dict=feed_dict)
            return [float(score) for score in score]

        if self.noans_token:
            yp1s, yp2s, prob, score = self.sess.run([self.yp1, self.yp2, self.yp_prob, self.noans_prob],
                                                    feed_dict=feed_dict)
            yp1s_noans, yp2s_noans = [], []
            for yp1, yp2 in zip(yp1s, yp2s):
                if yp1 == 0 or yp2 == 0:
                    yp1s_noans.append(-1)
                    yp2s_noans.append(-1)
                else:
                    yp1s_noans.append(yp1 - 1)
                    yp2s_noans.append(yp2 - 1)
            yp1s, yp2s = yp1s_noans, yp2s_noans
            return yp1s, yp2s, [float(p) for p in prob], [float(score) for score in score]

        if self.predict_ans and self.scorer:
            yp1s, yp2s, prob, logits, score = self.sess.run([self.yp1, self.yp2, self.yp_prob, self.yp_logits, self.noans_prob],
                                                            feed_dict=feed_dict)
            return yp1s, yp2s, [float(p) for p in prob], [float(logit) for logit in logits], [float(s) for s in score]

        yp1s, yp2s, prob, logits = self.sess.run([self.yp1, self.yp2, self.yp_prob, self.yp_logits],
                                                 feed_dict=feed_dict)
        return yp1s, yp2s, [float(p) for p in prob], [float(logit) for logit in logits]

    def process_event(self, event_name, data):
        if event_name == 'after_validation':
            # learning rate decay
            if data['impatience'] > self.last_impatience:
                self.lr_impatience += 1
            else:
                self.lr_impatience = 0

            self.last_impatience = data['impatience']

            if self.lr_impatience >= self.learning_rate_patience:
                self.lr_impatience = 0
                self.learning_rate = max(self.learning_rate / self.learning_rate_decay_factor, self.min_learning_rate)
                logger.info('SQuAD model: learning_rate changed to {}'.format(self.learning_rate))
            logger.info('SQuAD model: lr_impatience: {}, learning_rate: {}'.format(self.lr_impatience, self.learning_rate))
        elif event_name == 'before_validation':
            if self.weight_decay < 1.0:
                # validate model with EMA weights

                # save current weights to tmp path
                # warning: TFModel does not save optimizer params.
                # In our case, we do this save/load operation in one session so we do not lose optimizer params.
                self.save(path=self.tmp_model_path)
                # load ema weights
                self._assign_ema_weights()
        elif event_name == 'before_saving_improved_model':
            if self.weight_decay < 1.0:
                # load from tmp weights and do not call _assign_ema_weigts
                self.load(path=self.tmp_model_path)
                shutil.rmtree(self.tmp_model_path.parent)


    def _pad_strings(self, batch, len_limit):
        max_len = max(map(lambda x: len(x), batch))
        max_len = min(max_len, len_limit)
        for tokens in batch:
            tokens.extend([''] * (max_len - len(tokens)))
        return batch

    def shutdown(self):
        pass
