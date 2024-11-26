import argparse
import json
import logging
import os
import sys
import time
import yaml
from typing import Optional, List, Dict, Union

import concurrent.futures
import torch.distributed as dist

from lm_eval import utils
from lm_eval import evaluator as pretrain_evaluator
from lm_eval.tasks import TaskManager as PretrainTaskManager
from lm_eval.api.model import LM
from lm_eval.loggers import EvaluationTracker, WandbLogger
from lm_eval.loggers.utils import add_env_info, add_tokenizer_info, get_git_commit_hash
from lm_eval.utils import handle_non_serializable, simple_parse_args_string, sanitize_model_name
from lm_eval.__main__ import setup_parser, parse_eval_args
import lm_eval.api.metrics
import lm_eval.api.registry
import lm_eval.api.task
import lm_eval.models

import os
print(os.listdir())
print(os.getenv('PYTHONPATH'))

import sys
sys.path.append("./evalchemy")

from eval.task import TaskManager as InstructTaskManager
from eval.eval_tracker import DCEvaluationTracker


def setup_custom_parser():
    """
    Create a custom argument parser that extends lm-eval-harness parser.
    """
    parser = setup_parser()
    db_group = parser.add_mutually_exclusive_group()

    db_group.add_argument("--model_id", type=str, default=None, help="Model UUID for direct database tracking")

    parser.add_argument(
        "--use_database", action="store_true", help="Where to use PostgreSQL Database to track results."
    )
    parser.add_argument(
        "--model_name",
        type=str,
        default=None,
        help="Model name for direct database tracking. If not set, the model path will be used instead.",
    )

    db_group.add_argument(
        "--is_external_model",
        action="store_true",
        help="By default, the model is stored as internal in the database. If set, this is overwritten to external.",
    )

    parser.add_argument(
        "--creation_location",
        type=str,
        default="NA",
        help="Specifies which compute server is used for evaluating the model.",
    )

    parser.add_argument(
        "--created_by",
        type=str,
        default="NA",
        help="Specifies who evaluates the model.",
    )

    parser.add_argument(
        "--annotator_model",
        type=str,
        default="auto",
        help="Judge model used to evaluate generations. Example: gpt-4o-mini-2024-07-18",
    )

    parser.add_argument(
        "--config",
        type=str,
        help="Path to config yaml. Overwrites --batch_size, --tasks, and --annotator_model"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Run evalutaions in debug mode on a few examples",
    )
    return parser


def evaluate(
    lm: LM,
    task_manager: InstructTaskManager,
    pretrain_task_manager: PretrainTaskManager,
    task_list: List[str],
    batch_sizes_list: List[int],
    verbosity: str = "INFO",
    args=None,
    **eval_kwargs,
) -> Dict[str, Dict]:
    """
    Evaluate the language model on the given tasks.

    Args:
        lm (LM):
            Language model instance to evaluate.
        task_manager (InstructTaskManager):
            Manager for instruction-based evaluation tasks.
        pretrain_task_manager (PretrainTaskManager):
            Manager for pre-training evaluation tasks.
        task_list (List[str]):
            List of task names to evaluate the model on.
        batch_sizes_list (List[int]):
            List of batch sizes for each task.
        verbosity (str, optional):
            Logging verbosity level. Defaults to "INFO".
        args (Any, optional):
            Additional arguments to pass to the evaluation. Defaults to None.
        **eval_kwargs:
            Additional keyword arguments for evaluation configuration.

    Returns:
        Dict[str, Dict]:
            Dictionary mapping task names to their evaluation results.
            Each result dictionary contains metrics specific to that task.
    """
    eval_logger = utils.eval_logger
    eval_logger.setLevel(getattr(logging, f"{verbosity}"))

    # Split tasks between benchmark and pretrain
    benchmark_tasks = [t for t in task_list if t in task_manager.tasks]
    benchmark_batch_sizes = [b for (t,b) in zip(task_list, batch_sizes_list) if t in task_manager.tasks]
    pretrain_tasks = [t for t in task_list if t in pretrain_task_manager.all_tasks]
    pretrain_batch_sizes = [b for (t,b) in zip(task_list, batch_sizes_list) if t in pretrain_task_manager.all_tasks]

    unknown_tasks = set(task_list).difference(set(benchmark_tasks)).difference(set(pretrain_tasks))

    if len(unknown_tasks) > 0:
        raise ValueError(f"Tasks {unknown_tasks} are not recognized.")

    if benchmark_tasks:
        eval_logger.info(f"Benchmark tasks to evaluate: {benchmark_tasks}")
    if pretrain_tasks:
        eval_logger.info(f"Pretrain tasks to evaluate: {pretrain_tasks}")

    results = {"results": {}}

    # Run benchmark evaluations - sequential generation, parallel evaluation
    if benchmark_tasks:
        # Sequential generation since it's GPU-bound
        generate_methods = task_manager.get_list_generate_responses(benchmark_tasks)
        generation_results = []
        valid_tasks = []  # Keep track of valid tasks
        for method, task, batch_size in zip(generate_methods, benchmark_tasks, benchmark_batch_sizes):
            lm.batch_size_per_gpu = batch_size
            result = method(lm)
            if result is not None:  # Only keep valid results and their corresponding tasks
                generation_results.append(result)
                valid_tasks.append(task)
        # Get evaluation methods only for valid tasks
        evaluate_methods = task_manager.get_list_evaluates(valid_tasks)
        cpu_count = os.cpu_count()

        max_workers = min(len(valid_tasks), cpu_count * 2)
        if lm.world_size <= 1 or lm.rank == 0:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                evaluate_results = list(
                    executor.map(
                        lambda func_args: func_args[0](func_args[1]), zip(evaluate_methods, generation_results)
                    )
                )

            # Store results using valid tasks for correct mapping
            for task, result in zip(valid_tasks, evaluate_results):
                results["results"][task] = result

    # Run pretrain evaluations if any exist
    if pretrain_tasks and args is not None:
        try:
            for pretrain_task, batch_size in zip(pretrain_tasks, pretrain_batch_sizes):
                pretrain_results = pretrain_evaluator.simple_evaluate(
                    model=args.model,
                    model_args=args.model_args,
                    tasks=[pretrain_task],
                    num_fewshot=args.num_fewshot,
                    batch_size=batch_size,
                    max_batch_size=args.max_batch_size,
                    device=args.device,
                    use_cache=args.use_cache,
                    limit=args.limit,
                    check_integrity=args.check_integrity,
                    write_out=args.write_out,
                    log_samples=args.log_samples,
                    evaluation_tracker=args.evaluation_tracker if hasattr(args, "evaluation_tracker") else None,
                    system_instruction=args.system_instruction,
                    apply_chat_template=args.apply_chat_template,
                    fewshot_as_multiturn=args.fewshot_as_multiturn,
                    gen_kwargs=args.gen_kwargs,
                    task_manager=pretrain_task_manager,
                    verbosity=args.verbosity,
                    predict_only=args.predict_only,
                    random_seed=args.seed[0] if hasattr(args, "seed") else None,
                    numpy_random_seed=args.seed[1] if hasattr(args, "seed") else None,
                    torch_random_seed=args.seed[2] if hasattr(args, "seed") else None,
                    fewshot_random_seed=args.seed[3] if hasattr(args, "seed") else None,
                )
                if pretrain_results is not None:
                    results["results"].update(pretrain_results.get("results", {}))
        except Exception as e:
            eval_logger.error(f"Error in pretrain evaluation: {str(e)}")

    return results


def update_model_args_with_name(model_args: str, model_name: str) -> str:
    """
    Update model_args string to include pretrained model name if not already present.

    Args:
        model_args: Original model args string
        model_name: Model name to add

    Returns:
        str: Updated model args string
    """
    if not model_args:
        return f"pretrained={model_name}"

    args_dict = simple_parse_args_string(model_args)
    if "pretrained" not in args_dict:
        return f"pretrained={model_name},{model_args}"
    return model_args


def cli_evaluate(args: Optional[argparse.Namespace] = None) -> None:
    """
    Command-line interface for evaluating language models.

    Args:
        args: Command line arguments. If None, will parse from sys.argv
    """
    # Parse arguments if not provided
    if not args:
        parser = setup_custom_parser()
        args = parse_eval_args(parser)

    if args.config is not None:
        # This overwrites `--tasks` and `--batch_size`
        with open(args.config, "r") as file:
            tasks_yaml = yaml.safe_load(file)
        args.tasks = ','.join([t['task_name'] for t in tasks_yaml['tasks']])
        batch_sizes_list = [t['batch_size'] for t in tasks_yaml['tasks']]
        args.annotator_model = tasks_yaml.get('annotator_model', args.annotator_model)
    else:
        batch_sizes_list = [args.batch_size for _ in range(len(args.tasks.split(",")))]

    # Initialize evaluation tracker
    if args.output_path:
        args.hf_hub_log_args += f",output_path={args.output_path}"
    evaluation_tracker = setup_evaluation_tracker(args.output_path, args.use_database)

    # If model_id is provided, lookup model weights location from database
    if args.model_id:
        if not args.use_database:
            raise ValueError("--use_database must be set to use --model_id.")
        try:
            model_name = evaluation_tracker.get_model_attribute_from_db(args.model_id, "weights_location")
            args.model_args = update_model_args_with_name(args.model_args or "", model_name)
            utils.eval_logger.info(f"Retrieved model name from database: {model_name}")
        except Exception as e:
            utils.eval_logger.error(f"Failed to retrieve model name from database: {str(e)}")
            sys.exit(1)
    elif args.model_name:
        model_name = args.model_name
        args.model_args = update_model_args_with_name(args.model_args or "", model_name)

    # Initialize tasks
    task_list = args.tasks.split(",")
    task_manager = InstructTaskManager(annotator_model=args.annotator_model, debug=args.debug)
    pretrain_task_manager = PretrainTaskManager(args.verbosity, include_path=args.include_path)

    utils.eval_logger.info(f"Selected Tasks: {[task for task in task_list]}")

    # Initialize model
    try:
        lm = initialize_model(args.model, args.model_args)
    except Exception as e:
        utils.eval_logger.error(f"Failed to initialize model: {str(e)}")
        sys.exit(1)

    # Log experiment configuration
    if evaluation_tracker is not None:
        evaluation_tracker.general_config_tracker.log_experiment_args(
            model_source=args.model,
            model_args=args.model_args,
            system_instruction=args.system_instruction,
            chat_template=lm.chat_template(args.apply_chat_template),
            fewshot_as_multiturn=args.fewshot_as_multiturn,
        )

    # Initialize logging and environment
    eval_logger = utils.eval_logger
    eval_logger.setLevel(getattr(logging, f"{args.verbosity}"))
    os.environ["TOKENIZERS_PARALLELISM"] = "false"

    # Setup wandb logging if requested
    wandb_logger = None
    if args.wandb_args:
        wandb_logger = WandbLogger(**simple_parse_args_string(args.wandb_args))

    # Run evaluation
    results = evaluate(
        lm=lm,
        task_manager=task_manager,
        pretrain_task_manager=pretrain_task_manager,
        task_list=task_list,
        batch_sizes_list=batch_sizes_list,
        verbosity=args.verbosity,
        args=args,
    )

    # Add metadata to results
    if lm.rank == 0:
        add_results_metadata(results, batch_sizes_list, args, lm)
        handle_evaluation_output(results, args, evaluation_tracker, wandb_logger)

    if dist.is_initialized():
        dist.destroy_process_group()


def setup_evaluation_tracker(output_path: str, use_database: bool) -> DCEvaluationTracker:
    """
    This function initializes a DCEvaluationTracker instance with the specified
    configuration for either file-based or database storage of evaluation results.

    Args:
        output_path (str): The file system path where evaluation results will be saved.
            For file-based storage, this will be the directory path. For database
            storage, this could be the connection string or database path.
        use_database (bool): If True, uses database storage for results.
            If False, uses file-based storage.

    Returns:
        DCEvaluationTracker: A configured instance of the evaluation tracker
            ready to record and manage DCF evaluation results
    """
    return DCEvaluationTracker(output_path, use_database)


def initialize_model(
    model: Union[str, LM],
    model_args: Optional[str] = None,
    device: Optional[str] = None,
) -> LM:
    """
    Initialize the language model based on provided configuration.

    Args:
        model (Union[str, LM]):
            Either a string identifier for the model to load from registry,
            or an already instantiated LM object.
        model_args (Optional[str], optional):
            Additional arguments for model initialization as a string.
            Only used if model is provided as a string. Defaults to None.
        device (Optional[str], optional):
            Device to load the model on (e.g., 'cuda', 'cpu'). Defaults to None.

    Returns:
        LM:
            Initialized language model instance with configured parameters
            and a sanitized model identifier.
    """
    if isinstance(model, str):
        if model_args is None:
            model_args = ""

        config = {
            "device": device,
        }

        lm = lm_eval.api.registry.get_model(model).create_from_arg_string(
            model_args,
            config,
        )
    else:
        lm = model

    lm.model_identifier = sanitize_model_name(f"model_{model}_model_args_{model_args}")
    return lm


def add_results_metadata(results: Dict, batch_sizes_list: List[int], args: argparse.Namespace, lm: LM) -> None:
    """
    Add metadata and configuration to results.

    Args:
        results (Dict):
            Dictionary of evaluation results to be augmented with metadata.
            The function will modify this dictionary in-place to add
            configuration and runtime information.
        batch_sizes_list (List[int]):
            List of batch sizes for each task.
        args (argparse.Namespace):
            Command line arguments containing runtime configuration
            and parameters used during evaluation.
        lm (LM):
            Language model instance, used to extract model-specific
            configuration and parameters.

    Returns:
        None:
            The function modifies the results dictionary in-place.
    """
    results["config"] = {
        "model": (
            args.model
            if isinstance(args.model, str)
            else args.model.config._name_or_path if hasattr(args.model, "config") else type(args.model).__name__
        ),
        "model_args": args.model_args,
        "tasks": args.tasks,
        "batch_sizes": batch_sizes_list,
        "device": args.device,
        "use_cache": args.use_cache,
        "limit": args.limit,
        "annotator_model": args.annotator_model,
        # "bootstrap_iters": args.bootstrap_iters,
        "gen_kwargs": args.gen_kwargs,
        "random_seed": args.seed[0],
        "numpy_seed": args.seed[1],
        "torch_seed": args.seed[2],
        "fewshot_seed": args.seed[3],
    }

    if isinstance(lm, lm_eval.models.huggingface.HFLM):
        results["config"].update(lm.get_model_info())

    results["git_hash"] = get_git_commit_hash()
    results["date"] = time.time()
    add_env_info(results)
    add_tokenizer_info(results, lm)


def handle_evaluation_output(
    results: Dict,
    args: argparse.Namespace,
    evaluation_tracker: EvaluationTracker,
    wandb_logger: Optional[WandbLogger] = None,
) -> None:
    """
    Handle evaluation output, including logging and saving results.

    Args:
        results (Dict):
            Dictionary containing evaluation results for different tasks.
            Expected to map task names to their respective metric dictionaries.
        args (argparse.Namespace):
            Command line arguments containing configuration settings like
            output paths and logging preferences.
        evaluation_tracker (EvaluationTracker):
            Tracker object that maintains state and history of evaluation runs,
            used for metrics aggregation and progress monitoring.
        wandb_logger (Optional[WandbLogger], optional):
            Weights & Biases logger instance for experiment tracking and
            visualization. If None, W&B logging is disabled. Defaults to None.

    Returns:
        None:
            Function handles outputs via side effects (logging, saving files)
            rather than returning values.
    """
    if args.log_samples:
        samples = results.pop("samples")

    dumped = json.dumps(results, indent=2, default=handle_non_serializable, ensure_ascii=False)
    if args.show_config:
        print(dumped)

    batch_sizes = ",".join(map(str, results["config"]["batch_sizes"]))

    if wandb_logger:
        try:
            wandb_logger.post_init(results)
            wandb_logger.log_eval_result()
            if args.log_samples:
                wandb_logger.log_eval_samples(samples)
        except Exception as e:
            utils.eval_logger.info(f"Logging to Weights and Biases failed due to {e}")

    evaluation_tracker.save_results_aggregated(results=results, samples=samples if args.log_samples else None)
    if args.use_database and not args.debug:
        evaluation_tracker.update_evalresults_db(
            results,
            args.model_id,
            args.model_name,
            args.creation_location,
            args.created_by,
            args.is_external_model,
        )

    if args.log_samples:
        for task_name, config in results["configs"].items():
            evaluation_tracker.save_results_samples(task_name=task_name, samples=samples[task_name])

    utils.eval_logger.info(
        f"Eval arugments: {args.model} ({args.model_args}), gen_kwargs: ({args.gen_kwargs}), "
        f"limit: {args.limit}, num_fewshot: {args.num_fewshot}, annotator_model: {args.annotator_model}, "
        f"batch_size: {args.batch_size}{f' ({batch_sizes})' if batch_sizes else ''}"
    )

    if wandb_logger:
        wandb_logger.run.finish()


if __name__ == "__main__":
    cli_evaluate()
