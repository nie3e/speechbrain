"""Core SpeechBrain code for running experiments.

Authors
 * Peter Plantinga 2020
 * Abdel Heba 2020
"""

import os
import sys
import torch
import shutil
import logging
import inspect
import argparse
import subprocess
import speechbrain as sb
from datetime import date
from enum import Enum, auto
from tqdm.contrib import tqdm
from types import SimpleNamespace
from torch.nn import SyncBatchNorm
from torch.nn import DataParallel as DP
from torch.utils.data import DataLoader
from torch.utils.data import IterableDataset
from torch.utils.data import DistributedSampler
from speechbrain.data_io.batch import PaddedBatch
from speechbrain.data_io.dataset import DynamicItemDataset
from torch.nn.parallel import DistributedDataParallel as DDP
from speechbrain.data_io.dataloader import SaveableDataLoader
from speechbrain.data_io.sampler import DistributedSamplerWrapper

logger = logging.getLogger(__name__)
DEFAULT_LOG_CONFIG = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG_CONFIG = os.path.join(DEFAULT_LOG_CONFIG, "log-config.yaml")
torch._C._jit_set_profiling_executor(False)
torch._C._jit_set_profiling_mode(False)


def create_experiment_directory(
    experiment_directory,
    hyperparams_to_save=None,
    overrides={},
    log_config=DEFAULT_LOG_CONFIG,
    save_env_desc=True,
):
    """Create the output folder and relevant experimental files.

    Arguments
    ---------
    experiment_directory : str
        The place where the experiment directory should be created.
    hyperparams_to_save : str
        A filename of a yaml file representing the parameters for this
        experiment. If passed, references are resolved and the result is
        written to a file in the experiment directory called "hyperparams.yaml"
    overrides : dict
        A mapping of replacements made in the yaml file, to save in yaml.
    log_config : str
        A yaml filename containing configuration options for the logger.
    save_env_desc : bool
        If True, an environment state description is saved to the experiment
        directory, in a file called env.log in the experiment directory
    """
    try:
        # all writing command must be done with the main_process
        if sb.if_main_process():
            if not os.path.isdir(experiment_directory):
                os.makedirs(experiment_directory)

            # Write the parameters file
            if hyperparams_to_save is not None:
                hyperparams_filename = os.path.join(
                    experiment_directory, "hyperparams.yaml"
                )
                with open(hyperparams_to_save) as f:
                    resolved_yaml = sb.resolve_references(f, overrides)
                with open(hyperparams_filename, "w") as w:
                    print("# Generated %s from:" % date.today(), file=w)
                    print("# %s" % os.path.abspath(hyperparams_to_save), file=w)
                    print("# yamllint disable", file=w)
                    shutil.copyfileobj(resolved_yaml, w)

            # Copy executing file to output directory
            module = inspect.getmodule(inspect.currentframe().f_back)
            if module is not None:
                callingfile = os.path.realpath(module.__file__)
                shutil.copy(callingfile, experiment_directory)

            # Log exceptions to output automatically
            log_file = os.path.join(experiment_directory, "log.txt")
            logger_overrides = {
                "handlers": {"file_handler": {"filename": log_file}}
            }
            sb.utils.logger.setup_logging(log_config, logger_overrides)
            sys.excepthook = _logging_excepthook

            # Log beginning of experiment!
            logger.info("Beginning experiment!")
            logger.info(f"Experiment folder: {experiment_directory}")
            commit_hash = subprocess.check_output(
                ["git", "describe", "--always"]
            )
            logger.debug(
                "Commit hash: '%s'" % commit_hash.decode("utf-8").strip()
            )

            # Save system description:
            if save_env_desc:
                description_str = sb.utils.logger.get_environment_description()
                with open(
                    os.path.join(experiment_directory, "env.log"), "w"
                ) as fo:
                    fo.write(description_str)
    finally:
        # wait for main_process if ddp is used
        sb.ddp_barrier()


def _logging_excepthook(exc_type, exc_value, exc_traceback):
    """Interrupt exception raising to log the error."""
    logger.error("Exception:", exc_info=(exc_type, exc_value, exc_traceback))


def parse_arguments(arg_list):
    r"""Parse command-line arguments to the experiment.

    Arguments
    ---------
    arg_list: list
        a list of arguments to parse, most often from `sys.argv[1:]`

    Returns
    -------
    param_file : str
        The location of the parameters file.
    run_opts : dict
        Run options, such as distributed, device, etc.
    overrides : dict
        The overrides to pass to ``load_extended_yaml``.

    Example
    -------
    >>> argv = ['hyperparams.yaml', '--device', 'cuda:1', '--seed', '10']
    >>> filename, run_opts, overrides = parse_arguments(argv)
    >>> filename
    'hyperparams.yaml'
    >>> run_opts["device"]
    'cuda:1'
    >>> overrides
    'seed: 10'
    """
    parser = argparse.ArgumentParser(
        description="Run a SpeechBrain experiment",
    )
    parser.add_argument(
        "param_file",
        type=str,
        help="a yaml-formatted file using the extended YAML syntax "
        "defined by SpeechBrain.",
    )
    parser.add_argument(
        "--log_config",
        type=str,
        help="A file storing the configuration options for logging",
    )
    # if use_env = False in torch.distributed.lunch then local_rank arg is given
    parser.add_argument(
        "--local_rank", type=int, help="Rank on local machine",
    )
    parser.add_argument(
        "--device",
        type=str,
        default="cuda:0",
        help="The device to run the experiment on (e.g. 'cuda:0')",
    )
    parser.add_argument(
        "--data_parallel_count",
        type=int,
        default=-1,
        help="Number of devices that are used for data_parallel computation",
    )
    parser.add_argument(
        "--data_parallel_backend",
        type=bool,
        default=False,
        help="If True, data_parallel is used.",
    )
    parser.add_argument(
        "--distributed_launch",
        type=bool,
        default=False,
        help="if True, use DDP",
    )
    parser.add_argument(
        "--distributed_backend",
        type=str,
        default="nccl",
        help="One of {nccl, gloo, mpi}",
    )
    parser.add_argument(
        "--jit_module_keys",
        type=str,
        nargs="*",
        help="A list of keys in the 'modules' dict to jitify",
    )
    parser.add_argument(
        "--auto_mix_prec",
        type=bool,
        help="If True, automatic mixed-precision is used.",
    )
    parser.add_argument(
        "--max_grad_norm",
        type=float,
        help="Gradient norm will be clipped to this value, "
        "enter negative value to disable.",
    )
    parser.add_argument(
        "--nonfinite_patience",
        type=int,
        help="Max number of batches per epoch to skip if loss is nonfinite.",
    )
    parser.add_argument(
        "--progressbar",
        type=bool,
        help="If True, displays a progressbar indicating dataset progress.",
    )

    # Accept extra args to override yaml
    run_opts, overrides = parser.parse_known_args(arg_list)

    # Ignore items that are "None", they were not passed
    run_opts = {k: v for k, v in vars(run_opts).items() if v is not None}

    param_file = run_opts["param_file"]
    del run_opts["param_file"]

    overrides = _convert_to_yaml(overrides)

    # Checking that DataParallel use the right number of GPU
    if run_opts["data_parallel_backend"]:
        if run_opts["data_parallel_count"] == 0:
            raise ValueError(
                "data_parellel_count must be > 1."
                "if data_parallel_count = -1, then use all gpus."
            )
        if run_opts["data_parallel_count"] > torch.cuda.device_count():
            raise ValueError(
                "data_parellel_count must be <= "
                + str(torch.cuda.device_count())
                + "if data_parallel_count = -1, then use all gpus."
            )

    # For DDP, the device args must equal to local_rank used by torch.distributed.lunch
    # If run_opts["local_rank"] exists
    # Otherwise use OS.environ["LOCAL_RANK"]
    local_rank = None
    if "local_rank" in run_opts:
        local_rank = run_opts["local_rank"]
    else:
        if "LOCAL_RANK" in os.environ and os.environ["LOCAL_RANK"] != "":
            local_rank = int(os.environ["LOCAL_RANK"])

    # force device arg to be the same as local_rank from torch.distributed.lunch
    if local_rank is not None and "cuda" in run_opts["device"]:
        run_opts["device"] = run_opts["device"][:-1] + str(local_rank)

    return param_file, run_opts, overrides


def _convert_to_yaml(overrides):
    """Convert args to yaml for overrides"""
    yaml_string = ""

    # Handle '--arg=val' type args
    joined_args = "=".join(overrides)
    split_args = joined_args.split("=")

    for arg in split_args:
        if arg.startswith("--"):
            yaml_string += "\n" + arg[len("--") :] + ":"
        else:
            yaml_string += " " + arg

    return yaml_string.strip()


def if_main_process():
    """Check if the current process is the main process and authorized to run I/O commands.
    In DDP mode, the main process is the one with RANK == 0.
    In standard mode, the process will not have `RANK` Unix var and will be authorized to run the I/O commands.
    """
    if "RANK" in os.environ:
        if os.environ["RANK"] == "":
            return False
        else:
            if int(os.environ["RANK"]) == 0:
                return True
            return False
    return True


def ddp_barrier():
    """ In DDP mode, this function will synchronizes all processes.
    torch.distributed.barrier() will lock blocks processes until the whole group enters this function
    """
    if torch.distributed.is_initialized():
        torch.distributed.barrier()


def ddp_init_group(run_opts):
    """
    This function will initialize the ddp group if
    distributed_launch=True bool is given in the python command line.

    The ddp group will use distributed_backend arg for setting the DDP communication protocol.
    `RANK` Unix variable will be used for registring the subprocess to the ddp group.

    Arguments
    ---------
    run_opts: list
        a list of arguments to parse, most often from `sys.argv[1:]`
    """
    if run_opts["distributed_launch"]:
        if "local_rank" not in run_opts:
            sys.exit(
                "To use DDP backend, start your script with:\n\t"
                "python -m torch.distributed.lunch [args]\n\t"
                "experiment.py hyperparams.yaml --distributed_launch=True --distributed_backend=nccl"
            )
        else:
            if run_opts["local_rank"] + 1 > torch.cuda.device_count():
                sys.exit(
                    "Killing process " + str() + "\n"
                    "To use DDP backend, start your script with:\n\t"
                    "python -m torch.distributed.lunch [args]\n\t"
                    "experiment.py hyperparams.yaml --distributed_launch=True --distributed_backend=nccl"
                )
        if "RANK" in os.environ is None or os.environ["RANK"] == "":
            sys.exit(
                "To use DDP backend, start your script with:\n\t"
                "python -m torch.distributed.lunch [args]\n\t"
                "experiment.py hyperparams.yaml --distributed_launch=True --distributed_backend=nccl"
            )
        rank = int(os.environ["RANK"])

        if run_opts["distributed_backend"] == "nccl":
            if not torch.distributed.is_nccl_available():
                logger.info("NCCL is not supported in your machine.")
                raise ValueError("NCCL is not supported in your machine.")
        elif run_opts["distributed_backend"] == "gloo":
            if not torch.distributed.is_gloo_available():
                logger.info("GLOO is not supported in your machine.")
                raise ValueError("GLOO is not supported in your machine.")
        elif run_opts["distributed_backend"] == "mpi":
            if not torch.distributed.is_mpi_available():
                logger.info("MPI is not supported in your machine.")
                raise ValueError("MPI is not supported in your machine.")
        else:
            logger.info(
                run_opts["distributed_backend"]
                + " communcation protocol doesn't exist."
            )
            raise ValueError(
                run_opts["distributed_backend"]
                + " communcation protocol doesn't exist."
            )
        # rank arg is used to set the right rank of the current process for ddp.
        # if you have 2 servers with 2 gpu:
        # server1:
        #   GPU0: local_rank=device=0, rank=0
        #   GPU1: local_rank=device=1, rank=1
        # server2:
        #   GPU0: local_rank=device=0, rank=2
        #   GPU1: local_rank=device=1, rank=3
        torch.distributed.init_process_group(
            backend=run_opts["distributed_backend"], rank=rank,
        )
    else:
        logger.info(
            "Distributed_launch flag is disable, this experiment will be executed without DDP."
        )
        if "local_rank" in run_opts and run_opts["local_rank"] > 0:
            sys.exit(
                "DDP is disabled, no subprocess is accepted, signle GPU is then performed\n\t"
                "for multiGPU DDP training, please use --distributed_launch=True\n\t"
                "python -m torch.distributed.lunch [args]\n\t"
                "experiment.py hyperparams.yaml --distributed_launch=True --distributed_backend=nccl"
            )


class Stage(Enum):
    """Simple enum to track stage of experiments."""

    TRAIN = auto()
    VALID = auto()
    TEST = auto()


class Brain:
    r"""Brain class abstracts away the details of data loops.

    The primary purpose of the `Brain` class is the implementation of
    the ``fit()`` method, which iterates epochs and datasets for the
    purpose of "fitting" a set of modules to a set of data.

    In order to use the ``fit()`` method, one should sub-class the ``Brain``
    class and override any methods for which the default behavior does not
    match the use case. For a simple use case (e.g. training a single model
    with a single dataset) the only methods that need to be overridden are:

    * ``compute_forward()``
    * ``compute_objectives()``

    The example below illustrates how overriding these two methods is done.

    For more complicated use cases, such as multiple modules that need to
    be updated, the following methods can be overridden:

    * ``fit_batch()``
    * ``evaluate_batch()``

    Arguments
    ---------
    modules : dict of str:torch.nn.Module pairs
        These modules are passed to the optimizier by default if they have
        trainable parameters, and will have train()/eval() called on them.
    opt_class : torch.optim class
        A torch optimizer constructor that has takes only the list of
        parameters (e.g. a lambda or partial function definition). By default,
        this will be passed all modules in ``modules`` at the
        beginning of the ``fit()`` method. This behavior can be changed
        by overriding the ``configure_optimizers()`` method.
    hparams : dict
        Each key:value pair should consist of a string key and a hyperparameter
        that is used within the overridden methods. These will
        be accessible via an ``hparams`` attribute, using "dot" notation:
        e.g. self.hparams.model(x)
    run_opts : dict
        A set of options to change the runtime environment, including
            jit_module_keys : list of str
                List of keys in modules that should be jit compiled.
            distributed_count : int
                Number of devices to run on.
            distributed_backend : str
                One of {"ddp_nccl", "ddp_gloo", "ddp_mpi", "data_parallel"}
            device : str
                The location for performing computations.
            auto_mix_prec : bool
                If True, automatic mixed-precision is used.
                Activate it only with cuda.
            max_grad_norm : float
                Default implementation of ``fit_batch()`` uses
                ``clip_grad_norm_`` with this value.
            nonfinite_patience : int
                Number of times to ignore non-finite losses before stopping.
            progressbar : bool
                Whether to display a progressbar when training.
    checkpointer : speechbrain.Checkpointer
        By default, this will be used to load checkpoints, and will have the
        optimizer added to continue training if interrupted.

    Example
    -------
    >>> from torch.optim import SGD
    >>> class SimpleBrain(Brain):
    ...     def compute_forward(self, batch, stage):
    ...         return self.modules.model(batch[0])
    ...     def compute_objectives(self, predictions, batch, stage):
    ...         return torch.nn.functional.l1_loss(predictions, batch[0])
    >>> model = torch.nn.Linear(in_features=10, out_features=10)
    >>> brain = SimpleBrain({"model": model}, opt_class=lambda x: SGD(x, 0.1))
    >>> brain.fit(range(1), ([torch.rand(10, 10), torch.rand(10, 10)],))
    """

    def __init__(  # noqa: C901
        self,
        modules=None,
        opt_class=None,
        hparams=None,
        run_opts=None,
        checkpointer=None,
    ):
        self.opt_class = opt_class
        self.checkpointer = checkpointer

        # Arguments passed via the run opts dictionary
        run_opt_defaults = {
            "device": "cpu",
            "data_parallel_count": -1,
            "data_parallel_backend": False,
            "distributed_launch": False,
            "distributed_backend": "nccl",
            "jit_module_keys": None,
            "auto_mix_prec": False,
            "max_grad_norm": 5.0,
            "nonfinite_patience": 3,
            "progressbar": True,
        }
        for arg, default in run_opt_defaults.items():
            if run_opts is not None and arg in run_opts:
                if hparams is not None and arg in hparams:
                    logger.info(
                        "Info: " + arg + " arg overridden by command line input"
                    )
                setattr(self, arg, run_opts[arg])
            else:
                # If any arg from run_opt_defaults exist in hparams and
                # not in command line args "run_opts"
                if hparams is not None and arg in hparams:
                    logger.info(
                        "Info: " + arg + " arg from hparam file is used"
                    )
                    setattr(self, arg, hparams[arg])
                else:
                    setattr(self, arg, default)

        if self.data_parallel_backend and self.distributed_launch:
            sys.exit(
                "To use data_parallel backend, start you script with:\n\t"
                "python experiment.py hyperparams.yaml --data_parallel_backend=True --data_parallel_count=2"
                "To use DDP backend, start your script with:\n\t"
                "python -m torch.distributed.lunch [args]\n"
                "experiment.py hyperparams.yaml --distributed_launch=True --distributed_backend=nccl"
            )

        # Switch to the right context
        if "cuda" in self.device:
            torch.cuda.set_device(int(self.device[-1]))

        # Put modules on the right device, accessible with dot notation
        self.modules = torch.nn.ModuleDict(modules).to(self.device)

        # Make hyperparams available with dot notation too
        if hparams is not None:
            self.hparams = SimpleNamespace(**hparams)

        # Automatic mixed precision init
        if self.auto_mix_prec:
            self.scaler = torch.cuda.amp.GradScaler()

        # List parameter count for the user
        total_params = sum(
            p.numel() for p in self.modules.parameters() if p.requires_grad
        )
        if total_params > 0:
            clsname = self.__class__.__name__
            fmt_num = sb.utils.logger.format_order_of_magnitude(total_params)
            logger.info(f"{fmt_num} trainable parameters in {clsname}")

        if self.distributed_launch:
            self.rank = int(os.environ["RANK"])
            if not torch.distributed.is_initialized():
                if self.rank > 0:
                    sys.exit(
                        " ================ WARNING ==============="
                        "Please add sb.ddp_init_group() into your exp.py"
                        "To use DDP backend, start your script with:\n\t"
                        "python -m torch.distributed.lunch [args]\n\t"
                        "experiment.py hyperparams.yaml --distributed_launch=True --distributed_backend=nccl"
                    )
                else:
                    logger.warn(
                        "To use DDP, please add sb.ddp_init_group() into your exp.py"
                    )
                    logger.info(
                        "Only the main process is alive, all other subprocess were killed."
                    )
            # force the models to start and remain synchronized
            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.benchmark = False

    def compute_forward(self, x, stage):
        """Forward pass, to be overridden by sub-classes.

        Arguments
        ---------
        x : torch.Tensor or list of tensors
            The input tensor or tensors for processing.
        stage : Stage
            The stage of the experiment: Stage.TRAIN, Stage.VALID, Stage.TEST

        Returns
        -------
        torch.Tensor
            A tensor representing the outputs after all processing is complete.
        """
        raise NotImplementedError

    def compute_objectives(self, predictions, targets, stage):
        """Compute loss, to be overridden by sub-classes.

        Arguments
        ---------
        predictions : torch.Tensor or list of tensors
            The output tensor or tensors to evaluate.
        targets : torch.Tensor or list of tensors
            The gold standard to use for evaluation.
        stage : Stage
            The stage of the experiment: Stage.TRAIN, Stage.VALID, Stage.TEST

        Returns
        -------
        loss : torch.Tensor
            A tensor with the computed loss
        """
        raise NotImplementedError

    def on_stage_start(self, stage, epoch=None):
        """Gets called when a stage starts.

        Useful for defining class variables used during the stage.

        Arguments
        ---------
        stage : Stage
            The stage of the experiment: Stage.TRAIN, Stage.VALID, Stage.TEST
        epoch : int
            The current epoch count.
        """
        pass

    def on_stage_end(self, stage, stage_loss, epoch=None):
        """Gets called at the end of a stage.

        Arguments
        ---------
        stage : Stage
            The stage of the experiment: Stage.TRAIN, Stage.VALID, Stage.TEST
        stage_loss : float
            The average loss over the completed stage.
        epoch : int
            The current epoch count.
        """
        pass

    def handle_data_init(
        self,
        train_set,
        valid_set=None,
        train_sampler=None,
        shuffle_train=False,
        drop_last=False,
        dataloader_ckpt_pref="dataloader_",
        train_loader_kwargs=None,
        valid_loader_kwargs=None,
        **extra_loader_kwargs,
    ):
        """Creates DataLoaders for the datasets

        Arguments
        ---------
        train_set : Dataset
            A set of data to use for training.
        valid_set : Dataset
            A set of data to use for validation.
        train_sampler : Sampler, None
            Optional sampler to use for training data. In DDP, gets
            automatically wrapped in a DistributedSamplerWrapper.
        train_shuffle : bool
            To shuffle train data or not.
        drop_last : False
            Drop last incomplete batch and drop last uneven data in DDP.
        train_loader_kwargs : dict
            Additional keyword arguments to the training DataLoader. Some may be
            incompatible with other options, like train_sampler, shuffle_train.
        valid_loader_kwargs : dict
            Additional keyword arguments to the training DataLoader. Some may be
            incompatible with other options, like pin_memory.
        dataloader_ckpt_pref : str, None
            Prefix to use for SaveableDataLoader Checkpoint name. Set to None
            to not save the DataLoader.
        **extra_loader_kwargs : dict
            Extra keyword args to pass to both train and validation loaders.
            This can be used to specify e.g. pin_memory and num_workers for both.
        """
        if train_loader_kwargs is None:
            train_loader_kwargs = {}
        train_loader_kwargs["drop_last"] = drop_last
        if valid_loader_kwargs is None:
            valid_loader_kwargs = {}
        valid_loader_kwargs["drop_last"] = drop_last
        if train_sampler is not None and shuffle_train:
            raise ValueError(
                "Cannot specify both train_sampler and shuffle_train=True"
            )

        # DistributedSampler
        if self.distributed_launch:
            # num_replicas arg is equal to word_size
            # and retrieved automatically within
            # DistributedSampler obj.
            if train_sampler is not None:
                self.train_sampler = DistributedSamplerWrapper(
                    train_sampler, rank=self.rank, drop_last=drop_last
                )
            else:
                self.train_sampler = DistributedSampler(
                    train_set,
                    rank=self.rank,
                    shuffle=shuffle_train,
                    drop_last=drop_last,
                )
            train_loader_kwargs["sampler"] = self.train_sampler
        else:
            self.train_sampler = train_sampler

        # PaddedBatch as default collation for DynamicItemDataset
        if "collate_fn" not in train_loader_kwargs and isinstance(
            train_set, DynamicItemDataset
        ):
            train_loader_kwargs["collate_fn"] = PaddedBatch
        if "collate_fn" not in valid_loader_kwargs and isinstance(
            valid_set, DynamicItemDataset
        ):
            valid_loader_kwargs["collate_fn"] = PaddedBatch

        # Create the DataLoaders:
        train_loader_kwargs.update(
            {
                k: v
                for k, v in extra_loader_kwargs.items()
                if k not in train_loader_kwargs
            }
        )
        valid_loader_kwargs.update(
            {
                k: v
                for k, v in extra_loader_kwargs.items()
                if k not in valid_loader_kwargs
            }
        )
        if isinstance(train_set, IterableDataset):
            train_loader = DataLoader(train_set, **train_loader_kwargs)
        else:
            train_loader = SaveableDataLoader(train_set, **train_loader_kwargs)

        if valid_set is None:
            valid_loader = None
        elif isinstance(valid_set, IterableDataset):
            valid_loader = DataLoader(valid_set, **valid_loader_kwargs)
        else:
            valid_loader = SaveableDataLoader(valid_set, **valid_loader_kwargs)

        # Add DataLoaders to checkpointer:
        if self.checkpointer is not None and dataloader_ckpt_pref is not None:
            if isinstance(train_loader, SaveableDataLoader):
                train_name = dataloader_ckpt_pref + "train_set"
                self.checkpointer.add_recoverable(train_name, train_loader)
            if isinstance(valid_loader, SaveableDataLoader):
                valid_name = dataloader_ckpt_pref + "valid_set"
                self.checkpointer.add_recoverable(valid_name, valid_loader)

        return train_loader, valid_loader

    def on_fit_start(self):
        """Gets called at the beginning of ``fit()``, on multiple processes
        if distributed_count is more than 0 and backend is ddp.

        Default implementation compiles the jit modules, initializes
        optimizers, and loads the latest checkpoint to resume training.
        """
        # Run this *after* starting all processes since jit modules cannot be
        # pickled.
        self._compile_jit()

        # Wrap modules with parallel backend after jit
        self._wrap_distributed()

        # Initialize optimizers after parameters are configured
        self.init_optimizers()

        # Load latest checkpoint to resume training if interrupted
        if self.checkpointer is not None:
            self.checkpointer.recover_if_possible(
                device=torch.device(self.device)
            )

    def init_optimizers(self):
        """Called during ``on_fit_start()``, initialize optimizers
        after parameters are fully configured (e.g. DDP, jit).

        The default implementation of this method depends on an optimizer
        class being passed at initialization that takes only a list
        of parameters (e.g. a lambda or a partial function definition).
        This creates a single optimizer that optimizes all trainable params.

        Override this class if there are multiple optimizers.
        """
        if self.opt_class is not None:
            self.optimizer = self.opt_class(self.modules.parameters())

            if self.checkpointer is not None:
                self.checkpointer.add_recoverable("optimizer", self.optimizer)

    def on_evaluate_start(self, max_key=None, min_key=None):
        """Gets called at the beginning of ``evaluate()``

        Default implementation loads the best-performing checkpoint for
        evaluation, based on stored metrics.

        Arguments
        ---------
        max_key : str
            Key to use for finding best checkpoint (higher is better).
            By default, passed to ``self.checkpointer.recover_if_possible()``.
        min_key : str
            Key to use for finding best checkpoint (lower is better).
            By default, passed to ``self.checkpointer.recover_if_possible()``.
        """

        # Recover best checkpoint for evaluation
        if self.checkpointer is not None:
            self.checkpointer.recover_if_possible(
                max_key=max_key,
                min_key=min_key,
                device=torch.device(self.device),
            )

    def fit_batch(self, batch):
        """Fit one batch, override to do multiple updates.

        The default impementation depends on a few methods being defined
        with a particular behavior:

        * ``compute_forward()``
        * ``compute_objectives()``

        Also depends on having optimizers passed at initialization.

        Arguments
        ---------
        batch : list of torch.Tensors
            batch of data to use for training. Default implementation assumes
            this batch has two elements: inputs and targets.

        Returns
        -------
        detached loss
        """
        # Managing automatic mixed precision
        if self.auto_mix_prec:
            with torch.cuda.amp.autocast():
                outputs = self.compute_forward(batch, Stage.TRAIN)
                loss = self.compute_objectives(outputs, batch, Stage.TRAIN)
                self.scaler.scale(loss).backward()
                if self.check_gradients(loss):
                    self.scaler.step(self.optimizer)
                self.optimizer.zero_grad()
                self.scaler.update()
        else:
            outputs = self.compute_forward(batch, Stage.TRAIN)
            loss = self.compute_objectives(outputs, batch, Stage.TRAIN)
            loss.backward()
            if self.check_gradients(loss):
                self.optimizer.step()
            self.optimizer.zero_grad()

        return loss.detach().cpu()

    def check_gradients(self, loss):
        """Check if gradients are finite and not too large.

        Automatically clips large gradients.

        Arguments
        ---------
        loss : tensor
            The loss tensor after ``backward()`` has been called but
            before the optimizers ``step()``.

        Returns
        -------
        bool
            Whether or not the optimizer step should be carried out.
        """
        if not torch.isfinite(loss):
            self.nonfinite_count += 1

            # Print helpful debug info
            logger.warn(f"Loss is {loss}.")
            for p in self.modules.parameters():
                if not torch.isfinite(p).all():
                    logger.warn("Parameter is not finite: " + str(p))

            # Check if patience is exhausted
            if self.nonfinite_count > self.nonfinite_patience:
                raise ValueError(
                    "Loss is not finite and patience is exhausted. "
                    "To debug, wrap `fit()` with "
                    "autograd's `detect_anomaly()`, e.g.\n\nwith "
                    "torch.autograd.detect_anomaly():\n\tbrain.fit(...)"
                )
            else:
                logger.warn("Patience not yet exhausted, ignoring this batch.")
                return False

        # Clip gradient norm
        torch.nn.utils.clip_grad_norm_(
            (p for p in self.modules.parameters()), self.max_grad_norm
        )

        return True

    def evaluate_batch(self, batch, stage):
        """Evaluate one batch, override for different procedure than train.

        The default impementation depends on two methods being defined
        with a particular behavior:

        * ``compute_forward()``
        * ``compute_objectives()``

        Arguments
        ---------
        batch : list of torch.Tensors
            batch of data to use for evaluation. Default implementation assumes
            this batch has two elements: inputs and targets.
        stage : Stage
            The stage of the experiment: Stage.VALID, Stage.TEST

        Returns
        -------
        detached loss
        """

        out = self.compute_forward(batch, stage=stage)
        loss = self.compute_objectives(out, batch, stage=stage)
        return loss.detach().cpu()

    def fit(
        self,
        epoch_counter,
        train_set,
        valid_set=None,
        progressbar=None,
        **data_init_spec,
    ):
        """Iterate epochs and datasets to improve objective.

        Relies on the existence of mulitple functions that can (or should) be
        overridden. The following methods are used and expected to have a
        certain behavior:

        * ``fit_batch()``
        * ``evaluate_batch()``
        * ``update_average()``

        If the initialization was done with distributed_count > 0 and the
        distributed_backend is ddp, this will generally handle multiprocess
        logic, like splitting the training data into subsets for each device and
        only saving a checkpoint on the main process.

        Arguments
        ---------
        epoch_counter : iterable
            each call should return an integer indicating the epoch count.
        train_set : Dataset
            A set of data to use for training.
        valid_set : Dataset
            A set of data to use for validation.
        progressbar : bool
            Whether to display the progress of each epoch in a progressbar.
        """
        train_loader, valid_loader = self.handle_data_init(
            train_set, valid_set, **data_init_spec
        )

        self.on_fit_start()

        if progressbar is None:
            progressbar = self.progressbar

        # Iterate epochs
        for epoch in epoch_counter:

            # Training stage
            self.on_stage_start(Stage.TRAIN, epoch)
            self.modules.train()
            avg_train_loss = 0.0

            # Reset nonfinite count to 0 each epoch
            self.nonfinite_count = 0

            if self.train_sampler is not None:
                self.train_sampler.set_epoch(epoch)

            # Only show progressbar if requested and main_process
            disable = not (progressbar and sb.if_main_process())
            with tqdm(train_loader, dynamic_ncols=True, disable=disable) as t:
                for self.step, batch in enumerate(t):
                    loss = self.fit_batch(batch)
                    avg_train_loss = self.update_average(loss, avg_train_loss)
                    t.set_postfix(train_loss=avg_train_loss)
            self.on_stage_end(Stage.TRAIN, avg_train_loss, epoch)

            # Validation stage
            avg_valid_loss = None
            if valid_loader is not None:
                self.on_stage_start(Stage.VALID, epoch)
                self.modules.eval()
                avg_valid_loss = 0.0
                with torch.no_grad():
                    for self.step, batch in enumerate(
                        tqdm(valid_set, dynamic_ncols=True, disable=disable)
                    ):
                        loss = self.evaluate_batch(batch, stage=Stage.VALID)
                        avg_valid_loss = self.update_average(
                            loss, avg_valid_loss
                        )
                    try:
                        if sb.if_main_process():
                            self.on_stage_end(
                                Stage.VALID, avg_valid_loss, epoch
                            )
                    finally:
                        sb.ddp_barrier()

    def _compile_jit(self):
        """This should be run *after* mp.spawn, since jit modules
        cannot be pickled.
        """
        if self.jit_module_keys is None:
            return

        for name in self.jit_module_keys:
            if name not in self.modules:
                raise ValueError(
                    "module" + name + " is not defined in your hparams file."
                )
            module = torch.jit.script(self.modules[name])
            self.modules[name] = module.to(self.device)

    def _wrap_distributed(self):
        """Wrap modules with distributed wrapper when requested"""
        if not self.distributed_launch and not self.data_parallel_backend:
            return
        elif self.distributed_launch:
            for name, module in self.modules.items():
                if any(p.requires_grad for p in module.parameters()):
                    # for ddp, all module must run on same GPU
                    module = SyncBatchNorm.convert_sync_batchnorm(module)
                    module = DDP(module, device_ids=[self.device])
                    self.modules[name] = module
        else:
            # data_parallel_backend
            for name, module in self.modules.items():
                if any(p.requires_grad for p in module.parameters()):
                    # if distributed_count = -1 then use all gpus
                    # otherwise, specify the set of gpu to use
                    if self.data_parallel_count == -1:
                        module = DP(module)
                    else:
                        module = DP(
                            module,
                            [i for i in range(self.data_parallel_count)],
                        )
                    self.modules[name] = module

    def evaluate(self, test_set, max_key=None, min_key=None, progressbar=None):
        """Iterate test_set and evaluate brain performance. By default, loads
        the best-performing checkpoint (as recorded using the checkpointer).

        Arguments
        ---------
        test_set : list of DataLoaders
            This list will be zipped before iterating.
        max_key : str
            Key to use for finding best checkpoint, passed to on_evaluate_start
        min_key : str
            Key to use for finding best checkpoint, passed to on_evaluate_start
        progressbar : bool
            Whether to display the progress in a progressbar.

        Returns
        -------
        average test loss
        """
        if progressbar is None:
            progressbar = self.progressbar

        self.on_evaluate_start(max_key=max_key, min_key=min_key)
        self.on_stage_start(Stage.TEST, epoch=None)
        self.modules.eval()
        avg_test_loss = 0.0
        disable = not progressbar
        with torch.no_grad():
            for self.step, batch in enumerate(
                tqdm(test_set, dynamic_ncols=True, disable=disable)
            ):
                loss = self.evaluate_batch(batch, stage=Stage.TEST)
                avg_test_loss = self.update_average(loss, avg_test_loss)
            try:
                if sb.if_main_process():
                    self.on_stage_end(Stage.TEST, avg_test_loss, epoch=None)
            finally:
                sb.ddp_barrier()

    def update_average(self, loss, avg_loss):
        """Update running average of the loss.

        Arguments
        ---------
        loss : torch.tensor
            detached loss, a single float value.
        avg_loss : float
            current running average.

        Returns
        -------
        float
            The average loss
        """
        if torch.isfinite(loss):
            avg_loss -= avg_loss / (self.step + 1)
            avg_loss += float(loss) / (self.step + 1)
        return avg_loss
