from __future__ import unicode_literals

from collections import defaultdict

import numpy as np
import scipy.sparse as sp
from nlu_utils import normalize
from sklearn.feature_extraction.text import TfidfTransformer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.feature_selection import chi2

from snips_nlu.builtin_entities import is_builtin_entity
from snips_nlu.constants import ENTITIES, USE_SYNONYMS, SYNONYMS, VALUE, DATA
from snips_nlu.constants import NGRAM
from snips_nlu.languages import Language
from snips_nlu.preprocessing import stem
from snips_nlu.resources import get_stop_words, get_word_clusters, \
    get_stems
from snips_nlu.slot_filler.features_utils import get_all_ngrams
from snips_nlu.tokenization import tokenize_light


def default_tfidf_vectorizer(language):
    return TfidfVectorizer(tokenizer=lambda x: tokenize_light(x, language))


def get_tokens_clusters(tokens, language, cluster_name):
    clusters = get_word_clusters(language)[cluster_name]
    return [clusters[t] for t in tokens if t in clusters]


def entity_name_to_feature(entity_name):
    return "entityfeature%s" % "".join(tokenize_light(
        entity_name, language=Language.EN))


def normalize_stem(text, language):
    normalized_stemmed = normalize(text)
    if language in get_stems():
        normalized_stemmed = stem(normalized_stemmed, language)
    return normalized_stemmed


def get_word_cluster_features(query_tokens, language):
    cluster_name = CLUSTER_USED_PER_LANGUAGES.get(language, False)
    if not cluster_name:
        return []
    ngrams = get_all_ngrams(query_tokens)
    cluster_features = []
    for ngram in ngrams:
        cluster = get_word_clusters(language)[cluster_name].get(
            ngram[NGRAM].lower(), False)
        if cluster:
            cluster_features.append(cluster)
    return cluster_features


def get_dataset_entities_features(normalized_stemmed_tokens,
                                  entity_utterances_to_entity_names):
    ngrams = get_all_ngrams(normalized_stemmed_tokens)
    entity_features = []
    for ngram in ngrams:
        entity_features += [
            entity_name_to_feature(name) for name in
            entity_utterances_to_entity_names.get(ngram[NGRAM], [])]
    return entity_features


def preprocess_query(query, language, entity_utterances_to_entity_name):
    query_tokens = tokenize_light(query, language)
    word_clusters_features = get_word_cluster_features(query_tokens, language)
    normalized_stemmed_tokens = [normalize_stem(t, language)
                                 for t in query_tokens]
    entities_features = get_dataset_entities_features(
        normalized_stemmed_tokens, entity_utterances_to_entity_name)

    features = language.default_sep.join(normalized_stemmed_tokens)
    if len(entities_features):
        features += " " + " ".join(entities_features)
    if len(word_clusters_features):
        features += " " + " ".join(word_clusters_features)
    return features


def get_utterances_entities(dataset):
    entities_utterances = defaultdict(set)
    for entity_name, entity_data in dataset[ENTITIES].iteritems():
        if is_builtin_entity(entity_name):
            continue
        if entity_data[USE_SYNONYMS]:
            utterances = [u for ent in entity_data[DATA]
                          for u in [ent[VALUE]] + ent[SYNONYMS]]
        else:
            utterances = [ent[VALUE] for ent in entity_data[DATA]]
        for u in utterances:
            entities_utterances[u].add(entity_name)
    return dict(entities_utterances)


def deserialize_tfidf_vectorizer(vectorizer_dict, language):
    tfidf_vectorizer = default_tfidf_vectorizer(language)
    tfidf_vectorizer.vocabulary_ = vectorizer_dict["vocab"]
    idf_diag_data = np.array(vectorizer_dict["idf_diag"])
    idf_diag_shape = (len(idf_diag_data), len(idf_diag_data))
    row = range(idf_diag_shape[0])
    col = range(idf_diag_shape[0])
    idf_diag = sp.csr_matrix((idf_diag_data, (row, col)), shape=idf_diag_shape)
    tfidf_transformer = TfidfTransformer()
    tfidf_transformer._idf_diag = idf_diag
    tfidf_vectorizer._tfidf = tfidf_transformer
    return tfidf_vectorizer


CLUSTER_USED_PER_LANGUAGES = {}


class Featurizer(object):
    def __init__(self, language, tfidf_vectorizer=None, best_features=None,
                 entity_utterances_to_entity_names=None, pvalue_threshold=0.4):
        self.language = language
        if tfidf_vectorizer is None:
            tfidf_vectorizer = default_tfidf_vectorizer(self.language)
        self.tfidf_vectorizer = tfidf_vectorizer
        self.best_features = best_features
        self.pvalue_threshold = pvalue_threshold
        self.entity_utterances_to_entity_names = \
            entity_utterances_to_entity_names

    def preprocess_queries(self, queries):
        preprocessed_queries = []
        for q in queries:
            processed_query = preprocess_query(
                q, self.language, self.entity_utterances_to_entity_names)
            processed_query = processed_query.encode("utf8")
            preprocessed_queries.append(processed_query)
        return preprocessed_queries

    def fit(self, dataset, queries, y):
        utterance_entities = get_utterances_entities(dataset)
        entities = defaultdict(set)
        for u, entities_names in utterance_entities.iteritems():
            key = normalize_stem(u, self.language)
            entities[key].update(set(entities_names))

        self.entity_utterances_to_entity_names = entities

        if all(len("".join(tokenize_light(q, self.language))) == 0
               for q in queries):
            return None
        preprocessed_queries = self.preprocess_queries(queries)

        X_train_tfidf = self.tfidf_vectorizer.fit_transform(
            preprocessed_queries)
        list_index_words = {self.tfidf_vectorizer.vocabulary_[x]: x for x in
                            self.tfidf_vectorizer.vocabulary_}

        stop_words = get_stop_words(self.language)

        chi2val, pval = chi2(X_train_tfidf, y)
        self.best_features = [i for i, v in enumerate(pval) if
                              v < self.pvalue_threshold]
        if len(self.best_features) == 0:
            self.best_features = [idx for idx, val in enumerate(pval) if
                                  val == pval.min()]

        feature_names = {}
        for i in self.best_features:
            feature_names[i] = {'word': list_index_words[i], 'pval': pval[i]}

        for feat in feature_names:
            if feature_names[feat]['word'] in stop_words:
                if feature_names[feat]['pval'] > self.pvalue_threshold / 2.0:
                    self.best_features.remove(feat)

        return self

    def transform(self, queries):
        preprocessed_queries = self.preprocess_queries(queries)
        X_train_tfidf = self.tfidf_vectorizer.transform(preprocessed_queries)
        X = X_train_tfidf[:, self.best_features]
        return X

    def fit_transform(self, dataset, queries, y):
        return self.fit(dataset, queries, y).transform(queries)

    def to_dict(self):
        tfidf_vectorizer = {
            'vocab': self.tfidf_vectorizer.vocabulary_,
            'idf_diag': self.tfidf_vectorizer._tfidf._idf_diag.data.tolist()
        }
        entity_utterances_to_entity_names = {
            k: list(v)
            for k, v in self.entity_utterances_to_entity_names.iteritems()
        }
        return {
            'language_code': self.language.iso_code,
            'tfidf_vectorizer': tfidf_vectorizer,
            'best_features': self.best_features,
            'pvalue_threshold': self.pvalue_threshold,
            'entity_utterances_to_entity_names':
                entity_utterances_to_entity_names
        }

    @classmethod
    def from_dict(cls, obj_dict):
        language = Language.from_iso_code(obj_dict['language_code'])
        tfidf_vectorizer = deserialize_tfidf_vectorizer(
            obj_dict["tfidf_vectorizer"], language)
        entity_utterances_to_entity_names = {
            k: set(v) for k, v in
            obj_dict['entity_utterances_to_entity_names'].iteritems()
        }
        self = cls(
            language=language,
            tfidf_vectorizer=tfidf_vectorizer,
            pvalue_threshold=obj_dict['pvalue_threshold'],
            entity_utterances_to_entity_names=entity_utterances_to_entity_names,
            best_features=obj_dict['best_features']
        )
        return self
