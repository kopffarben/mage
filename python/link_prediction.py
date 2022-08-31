import mgp  # Python API
import json
import torch
import scipy
import dgl  # geometric deep learning
from dgl import AddSelfLoop, AddReverse, Compose
from typing import Callable, List, Tuple, Dict
from sklearn.model_selection import ParameterSampler
from numpy import int32
from dataclasses import dataclass, field
from collections import defaultdict
from heapq import heappop, heappush
from sklearn.metrics import precision_score, recall_score, average_precision_score
from mage.link_prediction import (
    add_self_loop,
    preprocess,
    classify,
    inner_train,
    inner_predict,
    create_model,
    create_optimizer,
    create_predictor,
    create_activation_function,
    proj_0,
    validate_user_parameters,
    Metrics,
    Predictors,
    Reindex,
    Context,
    Models,
    Optimizers,
    Devices,
    Aggregators,
    Parameters,
    Activations,
)

##############################
# classes and data structures
##############################


@dataclass
class LinkPredictionParameters:
    """Parameters user in LinkPrediction module.
    :param in_feats: int -> Defines the size of the input features. It will be automatically inferred by algorithm.
    :param hidden_features_size: List[int] -> Defines the size of each hidden layer in the architecture.
    :param layer_type: str -> Layer type
    :param num_epochs: int -> Number of epochs for model training
    :param optimizer: str -> Can be one of the following: ADAM, SGD, AdaGrad...
    :param learning_rate: float -> Learning rate for optimizer
    :param split_ratio: float -> Split ratio between training and validation set. There is not test dataset because it is assumed that user first needs to create new edges in dataset to test a model on them.
    :param node_features_property: str → Property name where the node features are saved.
    :param device_type: str ->  If model will be trained using CPU or cuda GPU. Possible values are cpu and cuda. To run it on Cuda, user must set this flag to true and system must support cuda execution.
        System's support is checked with torch.cuda.is_available()
    :param console_log_freq: int ->  how often do you want to print results from your model? Results will be from validation dataset.
    :param checkpoint_freq: int → Select the number of epochs on which the model will be saved. The model is persisted on disc.
    :param aggregator: str → Aggregator used in models. Can be one of the following: lstm, pool, mean, gcn. It is only used in graph_sage, not graph_attn
    :param metrics: mgp.List[str] -> Metrics used to evaluate model in training on the test/validation set(we don't use validation set to optimize parameters so everything is test set).
        Epoch will always be displayed, you can add loss, accuracy, precision, recall, specificity, F1, auc_score etc.
    :param predictor_type: str -> Type of the predictor. Predictor is used for combining node scores to edge scores.
    :param attn_num_heads: List[int] -> GAT can support usage of more than one head in each layers except last one. Only used in GAT, not in GraphSage.
    :param tr_acc_patience: int -> Training patience, for how many epoch will accuracy drop on validation set be tolerated before stopping the training.
    :param context_save_dir: str -> Path where the model and predictor will be saved every checkpoint_freq epochs.
    :param target_relation: str -> Unique edge type that is used for training.
    :param num_neg_per_pos_edge (int): Number of negative edges that will be sampled per one positive edge in the mini-batch.
    :param num_layers (int): Number of layers in the GNN architecture.
    :param batch_size (int): Batch size used in both training and validation procedure.
    :param sampling_workers (int): Number of workers that will cooperate in the sampling procedure in the training and validation.
    :param last_activation_function (str) → Activation function that is applied after the last layer in the model and before the predictor_type. Currently, only sigmoid is supported.
    :param add_reverse_edges (bool) -> Whether the module should add reverse edges for each in the obtained graph. If the source and destination nodes are of the same type, edges of the same edge type will
            be created. If the source and destination nodes are different, then prefix rev_ will be added to the previous edge type. Reverse edges will be excluded as message passing edges for corresponding supervision edges.
    :param add_self_loops (bool) -> Whether the module should add self loop edges to every node in the graph with edge_type set to "self".

    """

    in_feats: int = None
    hidden_features_size: List = field(
        default_factory=lambda: [128, 128]
    )  # Cannot add typing because of the way Python is implemented(no default things in dataclass, list is immutable something like this)
    layer_type: str = Models.GRAPH_ATTN
    num_epochs: int = 10
    optimizer: str = Optimizers.ADAM_OPT
    learning_rate: float = 0.01
    split_ratio: float = 0.8
    node_features_property: str = "features"
    device_type: str = Devices.CPU_DEVICE
    console_log_freq: int = 1
    checkpoint_freq: int = 10
    aggregator: str = Aggregators.POOL_AGG
    metrics: List = field(
        default_factory=lambda: [
            Metrics.LOSS,
            Metrics.ACCURACY,
            Metrics.AUC_SCORE,
            Metrics.PRECISION,
            Metrics.RECALL,
            Metrics.F1,
            Metrics.TRUE_POSITIVES,
            Metrics.FALSE_POSITIVES,
            Metrics.TRUE_NEGATIVES,
            Metrics.FALSE_NEGATIVES,
        ]
    )
    predictor_type: str = Predictors.MLP_PREDICTOR
    attn_num_heads: List[int] = field(default_factory=lambda: [4, 4])
    tr_acc_patience: int = 5
    context_save_dir: str = "/home/andi/Memgraph/code/mage/python/mage/link_prediction/context/"  # TODO: When the development finishes
    target_relation: str = None
    num_neg_per_pos_edge: int = 1
    batch_size: int = 512
    sampling_workers: int = 4
    last_activation_function = Activations.SIGMOID
    add_reverse_edges = False  # only allowed in some cases
    add_self_loops = False  # for automatically adding self-loop


##############################
# global parameters
##############################

link_prediction_parameters: LinkPredictionParameters = (
    LinkPredictionParameters()
)  # parameters currently saved.
training_results: List[
    Dict[str, float]
] = (
    list()
)  # List of all output training records. String is the metric's name and float represents value.
validation_results: List[
    Dict[str, float]
] = (
    list()
)  # List of all output validation results. String is the metric's name and float represents value in the Dictionary inside.
graph: dgl.graph = (
    None  # Reference to the graph. This includes training and validation.
)
reindex: Dict[
    str, Dict[str, Dict[int, int]]
] = None  # Mapping of DGL indexes to original dataset indexes for all node types and reverse.
predictor: torch.nn.Module = None  # Predictor for calculating edge scores
model: torch.nn.Module = None
labels_concat = (
    ":"  # string to separate labels if dealing with multiple labels per node
)
device = None  # reference to the device where the model will be executed, CPU or CUDA

# Lambda function to concat list of labels
merge_labels: Callable[[List[mgp.Label]], str] = lambda labels: labels_concat.join(
    [label.name for label in labels]
)

##############################
# All read procedures
##############################


@mgp.read_proc
def set_model_parameters(
    ctx: mgp.ProcCtx, parameters: mgp.Map
) -> mgp.Record(status=mgp.Any, message=str):
    """Saves parameters to the global parameters link_prediction_parameters. Specific parsing is needed because we want enable user to call it with a subset of parameters, no need to send them all.
    We will use some kind of reflection to most easily update parameters.

    Args:
        ctx (mgp.ProcCtx):  Reference to the context execution.
        hidden_features_size: mgp.List[int] -> Defines the size of each hidden layer in the architecture.
        layer_type: str -> Layer type
        num_epochs: int -> Number of epochs for model training
        optimizer: str -> Can be one of the following: ADAM, SGD, AdaGrad...
        learning_rate: float -> Learning rate for optimizer
        split_ratio: float -> Split ratio between training and validation set. There is not test dataset because it is assumed that user first needs to create new edges in dataset to test a model on them.
        node_features_property: str → Property name where the node features are saved.
        device_type: str ->  If model will be trained using CPU or cuda GPU. Possible values are cpu and cuda. To run it on Cuda, user must set this flag to true and system must support cuda execution.
                                System's support is checked with torch.cuda.is_available()
        console_log_freq: int ->  how often do you want to print results from your model? Results will be from validation dataset.
        checkpoint_freq: int → Select the number of epochs on which the model will be saved. The model is persisted on disc.
        aggregator: str → Aggregator used in models. Can be one of the following: lstm, pool, mean, gcn.
        metrics: mgp.List[str] -> Metrics used to evaluate model in training.
        predictor_type str: Type of the predictor. Predictor is used for combining node scores to edge scores.
        attn_num_heads: List[int] -> GAT can support usage of more than one head in each layer except last one. Only used in GAT, not in GraphSage.
        tr_acc_patience: int -> Training patience, for how many epoch will accuracy drop on test set be tolerated before stopping the training.
        context_save_dir: str -> Path where the model and predictor will be saved every checkpoint_freq epochs.
        target_relation: str -> Unique edge type that is used for training.
        num_neg_per_pos_edge: int -> Number of negative edges that will be sampled per one positive edge in the mini-batch.
        batch_size : Batch size used in both training and validation procedure.
        sampling_workers (int): Number of workers that will cooperate in the sampling procedure in the training and validation.
        last_activation_function (str) → Activation function that is applied after the last layer in the model and before the predictor_type. Currently, only sigmoid is supported.
        add_reverse_edges (bool) -> Whether the module should add reverse edges for each in the obtained graph. If the source and destination nodes are of the same type, edges of the same edge type will
            be created. If the source and destination nodes are different, then prefix rev_ will be added to the previous edge type. Reverse edges will be excluded as message passing edges for corresponding supervision edges.
        add_self_loops (bool) -> Whether the module should add self loop edges to every node in the graph with edge_type set to "self".

    Returns:
        mgp.Record:
            status (bool): True if parameters were successfully updated, False otherwise.
            message(str): Additional explanation why method failed or OK otherwise.
    """
    global link_prediction_parameters, device

    validate_user_parameters(parameters=parameters)

    for key, value in parameters.items():
        if not hasattr(link_prediction_parameters, key):
            return mgp.Record(
                status=False,
                message="Unknown parameter. ",
            )
        try:
            setattr(link_prediction_parameters, key, value)
        except Exception as exception:
            return mgp.Record(status=False, message=repr(exception))

    # Device type handling
    if (
        link_prediction_parameters.device_type == Devices.CUDA_DEVICE
        and torch.cuda.is_available() is True
    ):
        link_prediction_parameters.device_type = Devices.CUDA_DEVICE
        device = torch.device(Devices.CUDA_DEVICE)
    else:
        link_prediction_parameters.device_type = Devices.CPU_DEVICE
        device = torch.device(Devices.CPU_DEVICE)

    # Lists handling=generator expression + unpacking
    if type(link_prediction_parameters.hidden_features_size) == tuple:
        link_prediction_parameters.hidden_features_size = [
            *(x for x in link_prediction_parameters.hidden_features_size)
        ]

    if type(link_prediction_parameters.attn_num_heads) == tuple:
        link_prediction_parameters.attn_num_heads = [
            *(x for x in link_prediction_parameters.attn_num_heads)
        ]

    return mgp.Record(status=True, message="OK")


@mgp.read_proc
def train(
    ctx: mgp.ProcCtx,
) -> mgp.Record(training_results=mgp.Any, validation_results=mgp.Any):
    """Train method is used for training the module on the dataset provided with ctx. By taking decision to split the dataset here and not in the separate method, it is impossible to retrain the same model.

    Args:
        ctx (mgp.ProcCtx, optional): Reference to the process execution.

    Returns:
        mgp.Record: It returns performance metrics obtained during the training on the training and validation dataset.
    """
    # Get global context
    global training_results, validation_results, predictor, model, graph, reindex, link_prediction_parameters, device

    # Reset parameters of the old training
    _reset_train_predict_parameters()

    # Get some data
    # Dealing with heterogeneous graphs
    graph, reindex = _get_dgl_graph_data(
        ctx
    )  # dgl representation of the graph and dict new to old index

    # Insert in the hidden_features_size structure if needed
    if link_prediction_parameters.in_feats is None:
        # Get feature size
        ftr_size = max(
            graph.nodes[node_type]
            .data[link_prediction_parameters.node_features_property]
            .shape[1]
            for node_type in graph.ntypes
        )
        # Load feature size in the hidden_features_size
        _load_feature_size(ftr_size)

    # Split the data
    train_eid_dict, val_eid_dict = preprocess(
        graph=graph,
        split_ratio=link_prediction_parameters.split_ratio,
        target_relation=link_prediction_parameters.target_relation,
    )

    # Extract number of layers
    num_layers = len(link_prediction_parameters.hidden_features_size)

    # Create a model
    model = create_model(
        layer_type=link_prediction_parameters.layer_type,
        in_feats=link_prediction_parameters.in_feats,
        hidden_features_size=link_prediction_parameters.hidden_features_size,
        aggregator=link_prediction_parameters.aggregator,
        attn_num_heads=link_prediction_parameters.attn_num_heads,
        feat_drops=[0.09164 for _ in range(num_layers)],
        attn_drops=[0.09164 for _ in range(num_layers)],
        alphas=[0.512857 for _ in range(num_layers)],
        residuals=[True for _ in range(num_layers)],
        edge_types=graph.etypes,
        device=device,
    )

    # Create a predictor
    predictor = create_predictor(
        predictor_type=link_prediction_parameters.predictor_type,
        predictor_hidden_size=link_prediction_parameters.hidden_features_size[-1],
        device=device,
    )

    # Create an optimizer
    optimizer = create_optimizer(
        optimizer_type=link_prediction_parameters.optimizer,
        learning_rate=link_prediction_parameters.learning_rate,
        model=model,
        predictor=predictor,
    )

    # Create activation function
    m, threshold = create_activation_function(
        act_func=link_prediction_parameters.last_activation_function
    )

    # Call training method
    training_results, validation_results = inner_train(
        graph,
        train_eid_dict,
        val_eid_dict,
        link_prediction_parameters.target_relation,
        model,
        predictor,
        optimizer,
        link_prediction_parameters.num_epochs,
        m,
        threshold,
        link_prediction_parameters.node_features_property,
        link_prediction_parameters.console_log_freq,
        link_prediction_parameters.checkpoint_freq,
        link_prediction_parameters.metrics,
        link_prediction_parameters.tr_acc_patience,
        link_prediction_parameters.context_save_dir,
        link_prediction_parameters.num_neg_per_pos_edge,
        num_layers,
        link_prediction_parameters.batch_size,
        link_prediction_parameters.sampling_workers,
        device,
    )

    # Return results
    return mgp.Record(
        training_results=training_results, validation_results=validation_results
    )


@mgp.read_proc
def hyperparameter_tuning(
    ctx: mgp.ProcCtx, num_search_trials: int
) -> mgp.Record(
    best_parameters=mgp.Any,
    best_training_result=mgp.Any,
    best_validation_result=mgp.Any,
):
    """Optimize parameters. Function will not be enabled for user.

    Args:
        ctx (mgp.ProcCtx, optional): Reference to the process execution.
        num_search_trials (int): Number of search trials.

    Returns:
        mgp.Record: It returns performance metrics obtained during the training on the training and validation dataset.
    """
    global link_prediction_parameters, device

    # For saving best results
    best_parameters, best_training_result, best_validation_result = None, None, None
    delimiter = "****************************************"

    # Get some data
    # Dealing with heterogeneous graphs
    graph, _, = _get_dgl_graph_data(
        ctx
    )  # dgl representation of the graph and dict new to old index

    # Insert in the hidden_features_size structure if needed
    if link_prediction_parameters.in_feats is None:
        # Get feature size
        ftr_size = max(
            graph.nodes[node_type]
            .data[link_prediction_parameters.node_features_property]
            .shape[1]
            for node_type in graph.ntypes
        )
        print(f"Ftr size: {ftr_size}")
        # Load feature size in the hidden_features_size
        _load_feature_size(ftr_size)

    print(link_prediction_parameters.hidden_features_size)

    # Split the data
    train_eid_dict, val_eid_dict = preprocess(
        graph=graph,
        split_ratio=link_prediction_parameters.split_ratio,
        target_relation=link_prediction_parameters.target_relation,
    )

    # Specify search space
    gat_search_space = {
        Parameters.NUM_LAYERS: [1, 2, 3],
        Parameters.HIDDEN_FEATURES_SIZE: [32, 64, 128, 256],
        Parameters.ATTN_NUM_HEADS: [2, 4, 6],
        Parameters.DROPOUT: scipy.stats.uniform(0, 0.6),
        Parameters.ALPHA: scipy.stats.uniform(0, 0.6),
        Parameters.RESIDUAL: [True, False],
        Parameters.LEARNING_RATE: [0.0005, 0.001, 0.01, 0.1],
        Parameters.BATCH_SIZE: [128, 256, 512],
        Parameters.PREDICTOR_TYPE: [Predictors.MLP_PREDICTOR, Predictors.DOT_PREDICTOR],
    }

    configure_generator = ParameterSampler(gat_search_space, n_iter=num_search_trials)

    with open(
        "/home/andi/Memgraph/code/mage/python/mage/link_prediction/results.txt", "w"
    ) as f:
        for i, configure in enumerate(configure_generator):
            print("Configuration num: ", i + 1)
            print("Configuration: ", configure)
            num_layers = configure[Parameters.NUM_LAYERS]
            hidden_features_size = [
                configure[Parameters.HIDDEN_FEATURES_SIZE]
            ] * num_layers
            attn_num_heads = [configure[Parameters.ATTN_NUM_HEADS]] * num_layers
            dropouts = [configure[Parameters.DROPOUT]] * num_layers
            alphas = [configure[Parameters.ALPHA]] * num_layers
            residuals = [configure[Parameters.RESIDUAL]] * num_layers
            lr = configure[Parameters.LEARNING_RATE]
            batch_size = configure[Parameters.BATCH_SIZE]
            predictor_type = configure[Parameters.PREDICTOR_TYPE]

            # Create a model
            model = create_model(
                layer_type=Models.GRAPH_ATTN,
                in_feats=link_prediction_parameters.in_feats,
                hidden_features_size=hidden_features_size,
                aggregator=link_prediction_parameters.aggregator,  # only for gat
                attn_num_heads=attn_num_heads,
                feat_drops=dropouts,
                attn_drops=dropouts,
                alphas=alphas,
                residuals=residuals,
                edge_types=graph.etypes,
            )

            # Create a predictor
            predictor = create_predictor(
                predictor_type=predictor_type,
                predictor_hidden_size=hidden_features_size[-1],
            )

            # Create an optimizer
            optimizer = create_optimizer(
                optimizer_type=link_prediction_parameters.optimizer,
                learning_rate=lr,
                model=model,
                predictor=predictor,
            )

            # Create activation function
            m, threshold = create_activation_function(
                act_func=link_prediction_parameters.last_activation_function
            )

            # Call training method
            training_results, validation_results = inner_train(
                graph,
                train_eid_dict,
                val_eid_dict,
                link_prediction_parameters.target_relation,
                model,
                predictor,
                optimizer,
                link_prediction_parameters.num_epochs,
                m,
                threshold,
                link_prediction_parameters.node_features_property,
                link_prediction_parameters.console_log_freq,
                link_prediction_parameters.checkpoint_freq,
                link_prediction_parameters.metrics,
                link_prediction_parameters.tr_acc_patience,
                link_prediction_parameters.context_save_dir,
                link_prediction_parameters.num_neg_per_pos_edge,
                num_layers,
                batch_size,
                link_prediction_parameters.sampling_workers,
                device,
            )

            validation_result = validation_results[-1]

            f.write(json.dumps(configure) + "\n")
            f.write(json.dumps(validation_result) + "\n")
            f.write(delimiter + "\n")
            f.flush()

            if (
                best_validation_result is None
                or best_validation_result[Metrics.F1] < validation_result[Metrics.F1]
            ):
                best_training_result = training_results[-1]
                best_validation_result = validation_result
                best_parameters = configure
                print(best_validation_result)

    # Return results
    return mgp.Record(
        best_parameters=best_parameters,
        best_training_result=best_training_result,
        best_validation_result=best_validation_result,
    )


@mgp.read_proc
def predict(
    ctx: mgp.ProcCtx, src_vertex: mgp.Vertex, dest_vertex: mgp.Vertex
) -> mgp.Record(score=mgp.Number):
    """Predict method. It is assumed that nodes are added to the original Memgraph graph. It supports both situations, when the edge doesn't exist and when
    the edge exists.

    Args:
        ctx (mgp.ProcCtx): A reference to the context execution
        src_vertex (mgp.Vertex): Source vertex.
        dest_vertex (mgp.Vertex): Destination vertex.

    Returns:
        score: probability that two nodes are connected
    """
    global graph, predictor, model, reindex, link_prediction_parameters

    # If the model isn't available. Model is available if this method is called right after training or loaded from context.
    # Same goes for predictor.
    if model is None or predictor is None:
        raise Exception(
            "No trained model available to the system. Train or load it first. "
        )

    # Load graph again so you find nodes that were possibly added between train and prediction
    graph, reindex = _get_dgl_graph_data(
        ctx
    )  # dgl representation of the graph and dict new to old index

    # Create dgl graph representation
    src_old_id, src_type = src_vertex.id, merge_labels(src_vertex.labels)
    dest_old_id, dest_type = dest_vertex.id, merge_labels(dest_vertex.labels)

    # Check if src_type and dest_type are of the same target relation
    if type(link_prediction_parameters.target_relation) == tuple:
        if (
            src_type != link_prediction_parameters.target_relation[0]
            or dest_type != link_prediction_parameters.target_relation[2]
        ):
            raise Exception(
                "Prediction can be only computed on edges on which model was trained. "
            )
    else:
        for etype in graph.canonical_etypes:
            if link_prediction_parameters.target_relation == etype[1] and (
                etype[0] != src_type or etype[2] != dest_type
            ):
                raise Exception(
                    "Prediction can be only computed on edges on which model was trained. "
                )

    # Get dgl ids
    src_id = reindex[Reindex.MEMGRAPH][src_type][src_old_id]
    dest_id = reindex[Reindex.MEMGRAPH][dest_type][dest_old_id]

    # Init edge properties
    edge_added, edge_id = False, -1

    # Check if there is an edge between two nodes
    if not graph.has_edges_between(
        src_id, dest_id, etype=link_prediction_parameters.target_relation
    ):
        edge_added = True
        # print("Nodes {} and {} are not connected. ".format(src_old_id, dest_old_id))
        graph.add_edges(
            src_id, dest_id, etype=link_prediction_parameters.target_relation
        )

    edge_id = graph.edge_ids(
        src_id, dest_id, etype=link_prediction_parameters.target_relation
    )

    # Insert in the hidden_features_size structure if needed and it is needed only if the session was lost between training and predict method call.
    if link_prediction_parameters.in_feats is None:
        # Get feature size
        ftr_size = max(
            graph.nodes[node_type]
            .data[link_prediction_parameters.node_features_property]
            .shape[1]
            for node_type in graph.ntypes
        )
        # Load feature size in the hidden_features_size
        _load_feature_size(ftr_size)

    # Call utils module
    score = inner_predict(
        model=model,
        predictor=predictor,
        graph=graph,
        node_features_property=link_prediction_parameters.node_features_property,
        src_node=src_id,
        dest_node=dest_id,
        src_type=src_type,
        dest_type=dest_type,
    )

    if link_prediction_parameters.last_activation_function == Activations.SIGMOID:
        threshold = 0.5
    else:
        raise Exception(f"Currently, only {Activations.SIGMOID} is supported. ")

    result = mgp.Record(score=score)

    # Remove edge if necessary
    if edge_added:
        graph.remove_edges(edge_id, etype=link_prediction_parameters.target_relation)

    # print("Number of edges after: ", graph.number_of_edges())

    return result


@mgp.read_proc
def recommended_vertex(
    ctx: mgp.ProcCtx,
    src_vertex: mgp.Vertex,
    dest_vertices: mgp.List[mgp.Vertex],
    k: int,
) -> mgp.Record(score=mgp.Number, recommendation=mgp.Vertex):
    """Recommend method. It is assumed that nodes are already added to the original graph and our goal is to predict whether there is an edge between two nodes or not. Even if the edge exists,
     method can be used. Recommends k nodes based on edge scores.


    Args:
        ctx (mgp.ProcCtx): A reference to the context execution
        src_vertex (mgp.Vertex): Source vertex.
        dest_vertex (mgp.Vertex): Destination vertex.

    Returns:
        score: Probability that two nodes are connected
    """
    global graph, predictor, model, reindex, link_prediction_parameters

    print(f"Dest vertices: {len(dest_vertices)}")

    # If the model isn't available
    if model is None:
        raise Exception(
            "No trained model available to the system. Train or load it first. "
        )

    # You called predict after session was lost
    graph, reindex = _get_dgl_graph_data(ctx)

    # Insert in the hidden_features_size structure if needed and it is needed only if the session was lost between training and predict method call.
    if link_prediction_parameters.in_feats is None:
        # Get feature size
        ftr_size = max(
            graph.nodes[node_type]
            .data[link_prediction_parameters.node_features_property]
            .shape[1]
            for node_type in graph.ntypes
        )
        # Load feature size in the hidden_features_size
        _load_feature_size(ftr_size)

    # Create dgl graph representation
    src_old_id, src_type = src_vertex.id, merge_labels(src_vertex.labels)

    # Check if src_type is of the same target relation
    if type(link_prediction_parameters.target_relation) == tuple:
        if src_type != link_prediction_parameters.target_relation[0]:
            raise Exception(
                "Prediction can be only computed on edges on which model was trained. "
            )
    else:
        for etype in graph.canonical_etypes:
            if (
                link_prediction_parameters.target_relation == etype[1]
                and etype[0] != src_type
            ):
                raise Exception(
                    "Prediction can be only computed on edges on which model was trained. "
                )

    # Get dgl ids
    src_id = reindex[Reindex.MEMGRAPH][src_type][src_old_id]

    # Save if edge exists for every destination node by mapping dest old id to bool
    existing_edges: Dict[int, bool] = dict()

    # Save edge scores and vertices for each dest vertex.
    results: List[Tuple[float, int, mgp.Vertex]] = []

    for i, dest_vertex in enumerate(dest_vertices):
        # Get dest vertex
        dest_old_id, dest_type = dest_vertex.id, merge_labels(dest_vertex.labels)
        dest_id = reindex[Reindex.MEMGRAPH][dest_type][dest_old_id]

        # Check if dest_type is of the same target relation
        if type(link_prediction_parameters.target_relation) == tuple:
            if dest_type != link_prediction_parameters.target_relation[2]:
                raise Exception(
                    "Prediction can be only computed on edges on which model was trained. "
                )
        else:
            for etype in graph.canonical_etypes:
                if (
                    link_prediction_parameters.target_relation == etype[1]
                    and etype[2] != dest_type
                ):
                    raise Exception(
                        "Prediction can be only computed on edges on which model was trained. "
                    )

        # Init edge properties
        edge_added, edge_id = False, -1

        # Check if there is an edge between two nodes
        if not graph.has_edges_between(
            src_id, dest_id, etype=link_prediction_parameters.target_relation
        ):
            edge_added = True
            # print("Nodes {} and {} are not connected. ".format(src_old_id, dest_old_id))
            graph.add_edges(
                src_id, dest_id, etype=link_prediction_parameters.target_relation
            )
            existing_edges[dest_old_id] = False
        else:
            existing_edges[dest_old_id] = True

        edge_id = graph.edge_ids(
            src_id, dest_id, etype=link_prediction_parameters.target_relation
        )

        # Call utils module
        score = inner_predict(
            model=model,
            predictor=predictor,
            graph=graph,
            node_features_property=link_prediction_parameters.node_features_property,
            src_node=src_id,
            dest_node=dest_id,
            src_type=src_type,
            dest_type=dest_type,
        )

        # Remove edge if necessary
        if edge_added:
            graph.remove_edges(
                edge_id, etype=link_prediction_parameters.target_relation
            )

        heappush(
            results, (-score, i, dest_vertex)
        )  # Build in O(n). Add i to break ties where all predict values are the same.

    # Extract recommendations and metrics
    top_recommendations, top_scores, top_labels = (
        [],
        [],
        [],
    )  # scores=probability, recommendation=mgp.Vertex, labels save info if edges exist or not
    pop_size = min(k, len(results))

    if link_prediction_parameters.last_activation_function == Activations.SIGMOID:
        threshold = 0.5
    else:
        raise Exception(f"Currently, only {Activations.SIGMOID} is supported. ")

    for i in range(pop_size):
        score, i, recommendation = heappop(results)
        if -score < threshold:  # No need to continue because that is not predicted edge
            break
        # Handle vertex
        top_recommendations.append(recommendation)  # vertices
        # Handle score
        top_scores.append(-score)  # floats
        # Handle labels
        recommendation_old_id = recommendation.id
        if existing_edges[recommendation_old_id]:  # "relevant" edge
            top_labels.append(1)
        else:
            top_labels.append(0)

    # Update k value
    new_k = len(top_scores)

    # Calculate recommendation metrics
    top_scores_t = torch.tensor(top_scores)
    top_classes = classify(top_scores_t, threshold)
    precision_at_k = precision_score(top_labels, top_classes)
    recall_at_k = recall_score(top_labels, top_classes)
    f1_at_k = 2 * precision_at_k * recall_at_k / (precision_at_k + recall_at_k)
    ap = average_precision_score(top_labels, top_scores)  # average precision

    # Create final return results
    return_results = []
    for i in range(len(top_scores)):
        return_results.append(
            mgp.Record(score=top_scores[i], recommendation=top_recommendations[i])
        )

    print("*** Recommendation metrics ***")
    print(f"Precision@{new_k}: {round(precision_at_k, 3)}")
    print(f"Recall@{new_k}: {round(recall_at_k, 3)} ")
    print(f"F1@{new_k}: {round(f1_at_k, 3)}")
    print(f"AP: {round(ap, 3)}")

    return return_results


@mgp.read_proc
def get_training_results(
    ctx: mgp.ProcCtx,
) -> mgp.Record(training_results=mgp.Any, validation_results=mgp.Any):

    """This method is used when user wants to get performance data obtained from the last training. It is in the form of list of records where each record is a Dict[metric_name, metric_value]. Training and validation
    results are returned.

    Args:
        ctx (mgp.ProcCtx): Reference to the context execution

    Returns:
        mgp.Record[List[LinkPredictionOutputResult]]: A list of results. If the train method wasn't called yet, it returns empty lists.
    """
    global training_results, validation_results

    if training_results is None or validation_results is None:
        raise Exception("Training results are outdated or train method wasn't called. ")

    return mgp.Record(
        training_results=training_results, validation_results=validation_results
    )


@mgp.read_proc
def load_model(
    ctx: mgp.ProcCtx, path: str = link_prediction_parameters.context_save_dir
) -> mgp.Record(status=mgp.Any):
    """Loads torch model from given path. If the path doesn't exist, underlying exception is thrown.
    If the path argument is not given, it loads from the default path. If the user has changed path and the context was deleted
    then he/she needs to send that parameter here.

    Args:
        ctx (mgp.ProcCtx): A reference to the context execution.

    Returns:
        status(mgp.Any): True just to indicate that loading went well.
    """

    global model, predictor
    model = torch.load(path + Context.MODEL_NAME)
    predictor = torch.load(path + Context.PREDICTOR_NAME)
    return mgp.Record(status=True)


@mgp.read_proc
def reset_parameters(ctx: mgp.ProcCtx) -> mgp.Record(status=mgp.Any):
    """Resets all parameters.

    Args:
        ctx (mgp.ProcCtx): A reference to the execution context.

    Returns:
        status: True if all passed ok.
    """
    _reset_train_predict_parameters()
    return mgp.Record(status=True)


##############################
# Private helper methods.
##############################
def _load_feature_size(features_size: int):
    """Inserts feature size to the hidden_features_size array.

    Args:
        features_size (int): Features size.
    """
    global link_prediction_parameters
    link_prediction_parameters.in_feats = features_size


def _process_help_function(
    mem_indexes: Dict[str, int],
    old_index: int,
    type_: str,
    features: List[int],
    reindex: Dict[str, Dict[str, Dict[int, int]]],
    index_dgl_to_features: Dict[str, Dict[int, List[int]]],
) -> None:
    """Helper function for mapping original Memgraph graph to DGL representation.

    Args:
        mem_indexes (Dict[str, int]): Saves counters for each node type.
        old_index (int): Memgraph's node index.
        type_ (str): Node type.
        features (List[int]): Node features.
        reindex (Dict[str, Dict[str, Dict[int, int]]]): Mapping from original indexes to DGL indexes for each node type and reverse.
        index_dgl_to_features (Dict[str, Dict[int, List[int]]]): DGL indexes to features for each node type.
    """
    if type_ not in reindex[Reindex.DGL].keys():  # Node type not seen before
        reindex[Reindex.DGL][
            type_
        ] = dict()  # Mapping of old to new indexes for given type_
        reindex[Reindex.MEMGRAPH][type_] = dict()

    # Check if old_index has been seen for this label
    if old_index not in reindex[Reindex.MEMGRAPH][type_].keys():
        ind = mem_indexes[type_]  # get current counter
        reindex[Reindex.DGL][type_][ind] = old_index  # save new_to_old relationship
        reindex[Reindex.MEMGRAPH][type_][
            old_index
        ] = ind  # save old_to_new relationship
        # Check if list is given as a string
        if type(features) == str:
            index_dgl_to_features[type_][ind] = eval(
                features
            )  # Save new to features relationship. TODO: Remove that when we done with Cypher converting from String to List
        else:
            index_dgl_to_features[type_][ind] = features

        mem_indexes[type_] += 1


def _get_dgl_graph_data(
    ctx: mgp.ProcCtx,
) -> Tuple[dgl.graph, Dict[int32, int32], Dict[int32, int32]]:
    """Creates dgl representation of the graph. It works with heterogeneous and homogeneous.

    Args:
        ctx (mgp.ProcCtx): The reference to the context execution.

    Returns:
        Tuple[dgl.graph, Dict[str, Dict[int32, int32]], Dict[str, Dict[int32, int32]]: Tuple of DGL graph representation, dictionary of mapping new
        to old index and dictionary of mapping old to new index for each node type.
    """

    global link_prediction_parameters, device

    reindex = defaultdict(dict)  # map of label to new node index to old node index
    mem_indexes = defaultdict(
        int
    )  # map of label to indexes. All indexes are by default indexed 0.

    type_triplets = (
        []
    )  # list of tuples where each tuple is in following form(src_type, edge_type, dst_type), e.g. ("Customer", "SUBSCRIBES_TO", "Plan")
    index_dgl_to_features = defaultdict(dict)  # dgl indexes to features

    src_nodes, dest_nodes = defaultdict(list), defaultdict(
        list
    )  # label to node IDs -> Tuple of node-tensors format from DGL

    edge_types = set()

    isolated_nodes = []  # saves old ids

    # Iterate over all vertices
    for vertex in ctx.graph.vertices:
        # Process source vertex
        src_id, src_type, src_features = (
            vertex.id,
            merge_labels(vertex.labels),
            vertex.properties.get(link_prediction_parameters.node_features_property),
        )

        # Find if the node is disconnected from the rest of the graph
        src_isolated_node = True
        for _ in vertex.in_edges:  # Check incoming edges first
            src_isolated_node = False
            break

        if src_isolated_node:
            for _ in vertex.out_edges:  # Then check outgoing edges
                src_isolated_node = False
                break

        # If it isn't isolated node than map all indexes. Must be done before iterating over outgoing edges.
        if not src_isolated_node:
            _process_help_function(
                mem_indexes,
                src_id,
                src_type,
                src_features,
                reindex,
                index_dgl_to_features,
            )

        # Get all out edges
        for edge in vertex.out_edges:
            # Get edge information
            edge_type = edge.type.name

            # Process destination vertex next
            dest_node = edge.to_vertex
            dest_id, dest_type, dest_features = (
                dest_node.id,
                merge_labels(dest_node.labels),
                dest_node.properties.get(
                    link_prediction_parameters.node_features_property
                ),
            )

            # Define type triplet
            type_triplet = (src_type, edge_type, dest_type)
            # If this type triplet was already processed
            type_triplet_in = type_triplet in type_triplets

            # Before processing node dest node and edge, check if this edge_type has occurred with different src_type or dest_type
            if (
                edge_type in edge_types
                and not type_triplet_in
                and edge_type == link_prediction_parameters.target_relation
            ):
                raise Exception(
                    f"Edges of edge type {edge_type} are used for training and there are already edges with this edge type but with different combination of source and destination node. "
                )

            # Add to the type triplets
            if not type_triplet_in:
                type_triplets.append(type_triplet)

            # Add to the edge_types set
            edge_types.add(edge_type)

            # Handle mappings
            _process_help_function(
                mem_indexes,
                dest_id,
                dest_type,
                dest_features,
                reindex,
                index_dgl_to_features,
            )

            # Define edge
            src_nodes[type_triplet].append(reindex[Reindex.MEMGRAPH][src_type][src_id])
            dest_nodes[type_triplet].append(
                reindex[Reindex.MEMGRAPH][dest_type][dest_id]
            )

        # Append old id
        if src_isolated_node:
            isolated_nodes.append(src_id)

    # Check if there are no edges in the dataset, assume that it cannot learn effectively without edges. E2E handling.
    if len(src_nodes.keys()) == 0:
        raise Exception("No edges in the dataset. ")

    # data_dict has specific type that DGL requires to create a heterograph
    data_dict = dict()

    # Create a heterograph
    for type_triplet in type_triplets:
        data_dict[type_triplet] = torch.tensor(
            src_nodes[type_triplet], device=device
        ), torch.tensor(dest_nodes[type_triplet], device=device)

    g = dgl.heterograph(data_dict, device=device)

    # Infer automatically target relation if the graph is homogeneous and the user didn't provide its own target relation
    if len(type_triplets) == 1 and link_prediction_parameters.target_relation is None:
        link_prediction_parameters.target_relation = type_triplets[0]

    # Process isolated nodes by appending them to the end
    for isolated_node_id in isolated_nodes:
        isolated_node = ctx.graph.get_vertex_by_id(isolated_node_id)
        isolated_node_type, isolated_node_features = merge_labels(
            isolated_node.labels
        ), isolated_node.properties.get(
            link_prediction_parameters.node_features_property
        )
        _process_help_function(
            mem_indexes,
            isolated_node_id,
            isolated_node_type,
            isolated_node_features,
            reindex,
            index_dgl_to_features,
        )
        g.add_nodes(1, ntype=isolated_node_type)

    # Add undirected support if specified by user
    if link_prediction_parameters.add_reverse_edges:
        reverse_edges_transform = AddReverse(copy_edata=True, sym_new_etype=False)
        g = reverse_edges_transform(g)  # unfortunately copying is done

    # Custom made self-loop function is specified by the user.
    if link_prediction_parameters.add_self_loops:
        g = add_self_loop(g, "self")

    # print("After self loop transform")
    # for etype in g.canonical_etypes:
    #     print(f"Etype: {etype} Edges: {g.number_of_edges(etype=etype)} {g.edges(etype=etype)}")

    # Create features
    for node_type in g.ntypes:
        node_features = []
        for node in g.nodes(node_type):
            node_id = node.item()
            node_features.append(index_dgl_to_features[node_type][node_id])

        node_features = torch.tensor(node_features, dtype=torch.float32, device=device)
        g.nodes[node_type].data[
            link_prediction_parameters.node_features_property
        ] = node_features

    # Test conversion. Note: Do a conversion before you upscale features.
    _conversion_to_dgl_test(
        graph=g,
        reindex=reindex,
        ctx=ctx,
        node_features_property=link_prediction_parameters.node_features_property,
    )

    # Upscale features so they are all of same size
    proj_0(g, link_prediction_parameters.node_features_property)

    return g, reindex


def _reset_train_predict_parameters() -> None:
    """Reset global parameters that are returned by train method and used by predict method."""
    global training_results, validation_results, predictor, model, graph, reindex, device
    training_results = None  # clear training records from previous training
    validation_results = None  # clear validation record from previous training
    predictor = None  # Delete old predictor and create a new one in link_prediction_util.train method\
    model = None  # Annulate old model
    graph = None  # Set graph to None
    reindex = None  # Delete indexing stuff
    device = None  # user needs to set the device again


def _conversion_to_dgl_test(
    graph: dgl.graph,
    reindex: Dict[str, Dict[str, Dict[int, int]]],
    ctx: mgp.ProcCtx,
    node_features_property: str,
) -> None:
    """
    Tests whether conversion from ctx.ProcCtx graph to dgl graph went successfully. Checks how features are mapped. Throws exception if something fails.

    Args:
        graph (dgl.graph): Reference to the dgl heterogeneous graph.
        reindex (Dict[str, Dict[str, Dict[int, int]]]): Mapping from new indexes to old indexes for all node types and reverse.
        ctx (mgp.ProcCtx): Reference to the context execution.
        node_features_property (str): Property namer where the node features are saved`
    """

    # Check if the dataset is empty. E2E handling.
    if len(ctx.graph.vertices) == 0:
        raise Exception("The conversion to DGL failed. The dataset is empty. ")

    # Find all node types.
    for node_type in graph.ntypes:
        for vertex in graph.nodes(node_type):
            # Get int from torch.Tensor
            vertex_id = vertex.item()
            # Find vertex in Memgraph
            old_id = reindex[Reindex.DGL][node_type][vertex_id]
            vertex = ctx.graph.get_vertex_by_id(old_id)
            if vertex is None:
                raise Exception(
                    f"The conversion to DGL failed. Vertex with id {vertex.id} is not mapped to DGL graph. "
                )

            # Get features, check if they are given as string
            if type(vertex.properties.get(node_features_property)) == str:
                old_features = eval(
                    vertex.properties.get(node_features_property)
                )  # TODO: After dealing with Cypher modules
            else:
                old_features = vertex.properties.get(node_features_property)

            # Check if equal
            if not torch.equal(
                graph.nodes[node_type].data[node_features_property][vertex_id],
                torch.tensor(old_features, dtype=torch.float32, device=device),
            ):
                raise Exception(
                    "The conversion to DGL failed. Stored graph does not contain the same features as the converted DGL graph. "
                )
