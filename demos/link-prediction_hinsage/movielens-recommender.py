# -*- coding: utf-8 -*-
#
# Copyright 2018 Data61, CSIRO
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Graph link attribute prediction using HinSAGE, using the movielens data.
"""

import argparse
import pickle
import networkx as nx
from stellar.data.stellargraph import *
from stellar.mapper.link_mappers import *
from stellar.layer.hinsage import *
from stellar.layer.link_inference import link_regression
from keras import Input, Model, optimizers, losses, metrics
from typing import AnyStr, List
import json
from utils import ingest_graph, ingest_features, add_features_to_nodes
from sklearn import preprocessing, feature_extraction, model_selection
import pandas as pd
import multiprocessing


def read_graph(data_path, config_file):

    # Read the dataset config file:
    with open(config_file, "r") as f:
        config = json.load(f)

    # Read graph
    print("Reading graph...")
    gnx, id_map, inv_id_map = ingest_graph(data_path, config)

    # Read features
    print("Reading features...")
    user_features = ingest_features(data_path, config, node_type="users")
    movie_features = ingest_features(data_path, config, node_type="movies")

    # Prepare the user features for ML (movie features are already numeric and hence ML-ready):
    feature_names = ["age", "gender", "job"]

    feature_encoding = feature_extraction.DictVectorizer(sparse=False, dtype=int)
    feature_encoding.fit(user_features[feature_names].to_dict("records"))

    user_features_transformed = feature_encoding.transform(
        user_features[feature_names].to_dict("records")
    )
    user_features = pd.DataFrame(user_features_transformed, index=user_features.index)

    # Add the user and movie features to the graph:
    gnx = add_features_to_nodes(gnx, inv_id_map, user_features, movie_features)

    print(
        "Graph statistics: {} nodes, {} edges".format(
            gnx.number_of_nodes(), gnx.number_of_edges()
        )
    )

    return gnx


def root_mean_square_error(s_true, s_pred):
    return K.sqrt(K.mean(K.pow(s_true - s_pred, 2)))


class LinkInference(object):
    """
    Link attribute inference class
    """

    def __init__(self, g):
        self.g = g

    def train(
        self,
        layer_size: List[int],
        num_samples: List[int],
        train_size=0.7,
        batch_size: int = 1000,
        num_epochs: int = 10,
        learning_rate=1e-3,
        dropout=0.0,
        use_bias=True,
    ):
        """
        Build and train the HinSAGE model for link attribute prediction on the specified graph G
        with given parameters.

        Args:
            layer_size: a list of number of hidden nodes in each layer
            num_samples: number of neighbours to sample at each layer
            batch_size: size of mini batch
            num_epochs: number of epochs to train the model (epoch = all training batches are streamed through the model once)
            learning_rate: initial learning rate
            dropout: dropout probability in the range [0, 1)
            use_bias: tells whether to use a bias terms in HinSAGE model

        Returns:

        """

        # Training and test edges
        edges = list(self.g.edges(data=True))
        edges_train, edges_test = model_selection.train_test_split(
            edges, train_size=train_size
        )

        #  Edgelists:
        edgelist_train = [(e[0], e[1]) for e in edges_train]
        edgelist_test = [(e[0], e[1]) for e in edges_test]

        labels_train = [e[2]["score"] for e in edges_train]
        labels_test = [e[2]["score"] for e in edges_test]

        # Our machine learning task of learning user-movie ratings can be framed as a supervised Link Attribute Inference:
        # given a graph of user-movie ratings, we train a model for rating prediction using the ratings edges_train,
        # and evaluate it using the test ratings edges_test. The model also requires the user-movie graph structure.
        # To proceed, we need to create a StellarGraph object from the ingested graph, for training the model:
        # When sampling the GraphSAGE subgraphs, we want to treat user-movie links as undirected
        self.g = StellarGraph(self.g)
        # Make sure the StellarGraph object is ML-ready, i.e., that its node features are numeric (as required by the model):
        self.g.fit_attribute_spec()

        # Next, we create the link mappers for preparing and streaming training and testing data to the model.
        # The mappers essentially sample k-hop subgraphs of G with randomly selected head nodes, as required by
        # the HinSAGE algorithm, and generate minibatches of those samples to be fed to the input layer of the HinSAGE model.
        # Link mappers:
        mapper_train = HinSAGELinkMapper(
            self.g,
            edgelist_train,
            labels_train,
            batch_size,
            num_samples,
            name="mapper_train",
        )
        mapper_test = HinSAGELinkMapper(
            self.g,
            edgelist_test,
            labels_test,
            batch_size,
            num_samples,
            name="mapper_test",
        )

        assert mapper_train.type_adjacency_list == mapper_test.type_adjacency_list

        # Build the model by stacking a two-layer HinSAGE model and a link regression layer on top.
        assert len(layer_size) == len(
            num_samples
        ), "layer_size and num_samples must be of the same length! Stopping."
        hinsage = HinSAGE(
            layer_sizes=layer_size, mapper=mapper_train, bias=use_bias, dropout=dropout
        )

        # Define input and output sockets of hinsage:
        x_inp, x_out = hinsage.default_model()

        # Final estimator layer
        score_prediction = link_regression(
            edge_feature_method=args.edge_feature_method
        )(x_out)

        # Create Keras model for training
        model = Model(inputs=x_inp, outputs=score_prediction)
        model.compile(
            optimizer=optimizers.Adam(lr=learning_rate),
            loss=losses.mean_squared_error,
            metrics=[root_mean_square_error, metrics.mae],
        )

        # Train model
        print(
            "Training the model for {} epochs with initial learning rate {}".format(
                num_epochs, learning_rate
            )
        )
        history = model.fit_generator(
            mapper_train,
            validation_data=mapper_test,
            epochs=num_epochs,
            verbose=2,
            shuffle=True,
            use_multiprocessing=False,
            # workers=multiprocessing.cpu_count(),
        )

        # Evaluate and print metrics
        test_metrics = model.evaluate_generator(mapper_test)

        print("Test Evaluation:")
        for name, val in zip(model.metrics_names, test_metrics):
            print("\t{}: {:0.4f}".format(name, val))

    def test(self, G: StellarGraphBase, model_file: AnyStr):
        pass


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="Run GraphSAGE on movielens")

    parser.add_argument(
        "--data_path", type=str, default="../data/ml-100k", help="Data path."
    )
    parser.add_argument(
        "-f",
        "--config",
        type=str,
        default="ml-100k-config.json",
        help="Data config file.",
    )
    parser.add_argument(
        "-t",
        "--target",
        type=str,
        default="score",
        help="The target edge attribute, default is 'score'",
    )
    parser.add_argument(
        "-m",
        "--edge_feature_method",
        type=str,
        default="ip",
        help="The method for combining node embeddings into edge embeddings: 'concat', 'mul', or 'ip",
    )
    parser.add_argument(
        "-r",
        "--learningrate",
        type=float,
        default=0.0005,
        help="Learning rate for training model",
    )
    parser.add_argument(
        "-n", "--batch_size", type=int, default=500, help="Load a save checkpoint file"
    )
    parser.add_argument(
        "-e", "--epochs", type=int, default=10, help="Number of epochs to train for"
    )
    parser.add_argument(
        "-s",
        "--neighbour_samples",
        type=int,
        nargs="*",
        default=[2, 5],
        help="The number of nodes sampled at each layer",
    )
    parser.add_argument(
        "-l",
        "--layer_size",
        type=int,
        nargs="*",
        default=[50, 50],
        help="The number of hidden features at each layer",
    )
    parser.add_argument(
        "-d",
        "--dropout",
        type=float,
        default=0.0,
        help="Dropout for the HinSAGE model, between 0.0 and 1.0",
    )
    parser.add_argument(
        "-c",
        "--checkpoint",
        nargs="?",
        type=str,
        default=None,
        help="Load a checkpoint file",
    )

    args, cmdline_args = parser.parse_known_args()

    G = read_graph(args.data_path, args.config)

    model = LinkInference(G)

    if args.checkpoint is None:
        model.train(
            train_size=0.7,
            learning_rate=args.learningrate,
            layer_size=args.layer_size,
            num_samples=args.neighbour_samples,
            batch_size=args.batch_size,
            num_epochs=args.epochs,
            dropout=args.dropout,
        )
    else:
        model.test(args.checkpoint)
