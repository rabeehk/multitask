# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import glob
import os
from dataclasses import asdict
from logging import getLogger
from third_party.utils import (
    assert_all_frozen,
    freeze_embeds,
    freeze_params,
    save_json)
from transformers import TrainerCallback
from transformers.modeling_t5 import T5LayerNorm

from seq2seq.adapters import (AdapterController, MetaAdapterController,
                              LayerNormHyperNet, AdapterLayersHyperNetController)
from seq2seq.data import TASK_MAPPING

logger = getLogger(__name__)


def create_dir(output_dir):
    """
    Checks whether to the output_dir already exists and creates it if not.
    Args:
      output_dir: path to the output_dir
    """
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)


def handle_metrics(split, metrics, output_dir, gcs_bucket=None):
    """
    Prints and saves metrics or a general dictionary of results.

    Args:
        split: one of train, val, test, or training arguments.
        metrics: metrics dict
        output_dir: where to save the metrics, if gcs_bucket is given
        we save the results also on the given bucket.
    """
    logger.info(f"***** {split} metrics *****")
    for key in sorted(metrics.keys()):
        logger.info(f"  {key} = {metrics[key]}")
    save_json_file(metrics, f"{split}_results.json", output_dir, gcs_bucket)


def save_json_file(json_dict, outfile_name, output_dir, gcs_bucket=None):
    """
    Saves the given dictionary as a json file to output_dir and also
    the given bucket if given.
    """
    save_json(json_dict, os.path.join(output_dir, outfile_name))
    if gcs_bucket is not None:
        logger.info("***** Uploading results into gs-bucket *****")
        upload(output_dir, gcs_bucket)


def get_training_args(arguments_list):
    """
    Concatenate all training arguments except evaluation strategy which
    is not Json serializable.
    Args:
        arguments_list: list of dataclasses.
    Return:
        arguments: concatenated arguments.
    """
    all_arguments = {}
    for arguments in arguments_list:
        all_arguments.update(asdict(arguments))
    all_arguments.pop("evaluation_strategy")
    return all_arguments


def get_last_checkpoint_path(output_dir):
    """
    Finds the path for the last checkpoint saved in the output_dir
    Args:
        output_dir:  output_dir
    Returns:
        path to the last checkpoint saved in the output dir.
    """
    paths = glob.glob(os.path.join(output_dir, "checkpoint-*"))
    if len(paths) == 0:
        return output_dir
    else:
        checkpoints = [int(checkpoint.split('-')[-1]) for checkpoint in paths]
        max_checkpoint = max(checkpoints)
        return os.path.join(output_dir, "checkpoint-" + str(max_checkpoint))


def upload(upload_dir: str, gcs_bucket: str) -> None:
    """Uploads the local upload_dir to the gs bucket."""
    try:
        os.system("/root/google-cloud-sdk/bin/gsutil -m rm -r {}".format(
            os.path.join("gs://" + gcs_bucket, upload_dir)))
        if os.system("/root/google-cloud-sdk/bin/gsutil -m cp -r {} {}".format(
                upload_dir,
                os.path.join("gs://" + gcs_bucket, upload_dir))) != 0:
            raise Exception('gsutil path not found')

    except:
        os.system("gsutil -m rm -r {}".format(
            os.path.join("gs://" + gcs_bucket, upload_dir)))
        os.system("gsutil -m cp -r {} {}".format(
            upload_dir,
            os.path.join("gs://" + gcs_bucket, upload_dir)))


def use_task_specific_params(model, task):
    """Update config with task specific params during evaluation."""
    task_dataset = TASK_MAPPING[task]
    task_specific_config = task_dataset.task_specific_config
    if task_specific_config is not None:
        logger.info(f"using task specific params for {task}: {task_specific_config}")
        model.config.update(task_specific_config)


def reset_config(model, config):
    """Resets the config file to the one provided."""
    model.config = config
    logger.info(f"config is reset to the initial values.")


def freezing_params(model, training_args, model_args, adapter_args):
    """
    Freezes the model parameters based on the given setting in the arguments.
    Args:
      model: the given model.
      training_args: defines the training arguments.
      model_args: defines the model arguments.
      adapter_args: defines the adapters arguments.
    """
    # If we are training adapters, we freeze all parameters except the
    # parameters of computing task embeddings and adapter controllers.
    if training_args.train_adapters:
        freeze_params(model)
        for name, sub_module in model.named_modules():
            if isinstance(sub_module, (MetaAdapterController, AdapterController)):
                for param_name, param in sub_module.named_parameters():
                    param.requires_grad = True
        if adapter_args.adapter_config_name == "meta-adapter":
            for param in model.task_embedding_controller.parameters():
                param.requires_grad = True
        if adapter_args.unique_hyper_net:
            for name, sub_module in model.named_modules():
                if isinstance(sub_module, (AdapterLayersHyperNetController, AdapterController)):
                    for param_name, param in sub_module.named_parameters():
                        param.requires_grad = True

    if model_args.freeze_model:
        freeze_params(model)

    # Freezes all models parameters except last linear layer of decoder.
    if model_args.freeze_model_but_lm_head:
        freeze_params(model)
        for param in model.lm_head.parameters():
            param.requires_grad = True

    if model_args.freeze_embeds:
        freeze_embeds(model)

    if model_args.freeze_encoder:
        freeze_params(model.get_encoder())
        assert_all_frozen(model.get_encoder())

    # In case of meta-adapters and if task-embeddings are paramteric,
    # freezes all parameters except task-embedding parameters.
    if model_args.freeze_model_but_task_embeddings:
        freeze_params(model)
        if adapter_args.adapter_config_name == "meta-adapter" and \
                not isinstance(model.task_embedding_controller.task_to_embeddings, dict):
            for param in model.task_embedding_controller.task_to_embeddings.parameters():
                param.requires_grad = True

    # Unfreezes last linear layer of decoder.
    if model_args.unfreeze_lm_head:
        for param in model.lm_head.parameters():
            param.requires_grad = True

    # Unfreezes layer norms.
    if model_args.unfreeze_layer_norms:
        for name, sub_module in model.named_modules():
            if isinstance(sub_module, T5LayerNorm):
                for param_name, param in sub_module.named_parameters():
                    param.requires_grad = True

    if adapter_args.conditional_layer_norm_for_T5:
        for name, sub_module in model.named_modules():
            if isinstance(sub_module, LayerNormHyperNet):
                for param_name, param in sub_module.named_parameters():
                    param.requires_grad = True


class T5CheckpointCallback(TrainerCallback):
    """Uploads the output_dir to the gs_bucket."""

    def on_save(self, args, state, control, **kwargs):
        """Event called after a checkpoint save."""
        if state.is_world_process_zero and args.gcs_bucket is not None:
            upload(args.output_dir, args.gcs_bucket)
