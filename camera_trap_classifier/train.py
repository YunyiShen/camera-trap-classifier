""" Main File for Training a Model

Example Usage:
---------------
ctc.train \
-train_tfr_path ./test_big/cats_vs_dogs/tfr_files \
-train_tfr_pattern train \
-val_tfr_path ./test_big/cats_vs_dogs/tfr_files \
-val_tfr_pattern val \
-test_tfr_path ./test_big/cats_vs_dogs/tfr_files \
-test_tfr_pattern test \
-class_mapping_json ./test_big/cats_vs_dogs/tfr_files/label_mapping.json \
-run_outputs_dir ./test_big/cats_vs_dogs/run_outputs/ \
-model_save_dir ./test_big/cats_vs_dogs/model_save_dir/ \
-model small_cnn \
-labels class \
-batch_size 128 \
-n_cpus 2 \
-n_gpus 1 \
-buffer_size 512 \
-max_epochs 10 \
-starting_epoch 0 \
-optimizer sgd
"""
import argparse
import logging
import os
import textwrap

import tensorflow as tf
import numpy as np
from tensorflow.python.keras.callbacks import (
    TensorBoard, EarlyStopping, CSVLogger, ReduceLROnPlateau)

from camera_trap_classifier.training.hooks import (
    TableInitializerCallback, ModelCheckpoint)
from camera_trap_classifier.config.config import ConfigLoader
from camera_trap_classifier.config.logging import setup_logging
from camera_trap_classifier.training.utils import copy_models_and_config_files
from camera_trap_classifier.training.prepare_model import create_model
from camera_trap_classifier.predicting.predictor import Predictor
from camera_trap_classifier.data.tfr_encoder_decoder import (
    DefaultTFRecordEncoderDecoder)
from camera_trap_classifier.data.reader import DatasetReader
from camera_trap_classifier.data.image import preprocess_image
from camera_trap_classifier.data.utils import (
    calc_n_batches_per_epoch, export_dict_to_json, read_json,
    n_records_in_tfr_dataset, find_files_with_ending,
    get_most_recent_file_from_files, find_tfr_files_pattern_subdir)


def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-train_tfr_path", type=str, required=True,
        help="Path to directory that contains the training TFR files \
              (incl. subdirs)")
    parser.add_argument(
        "-train_tfr_pattern", nargs='+', type=str,
        help="The pattern of the training TFR files (default train) \
              list of 1 or more patterns that all have to match",
        default=['train'], required=False)
    parser.add_argument(
        "-val_tfr_path", type=str, required=True,
        help="Path to directory that contains the validation TFR files \
              (incl. subdirs)")
    parser.add_argument(
        "-val_tfr_pattern", nargs='+', type=str,
        help="The pattern of the validation TFR files (default val) \
              list of 1 or more patterns that all have to match",
        default=['val'], required=False)
    parser.add_argument(
        "-test_tfr_path", type=str, required=False,
        help="Path to directory that contains the test TFR files \
              (incl. subdirs - optional)")
    parser.add_argument(
        "-test_tfr_pattern", nargs='+', type=str,
        help="The pattern of the test TFR files (default test) \
              list of 1 or more patterns that all have to match",
        default=['test'], required=False)
    parser.add_argument(
        "-class_mapping_json", type=str, required=True,
        help='Path to the json file containing the class mappings')
    parser.add_argument(
        "-run_outputs_dir", type=str, required=True,
        help="Path to a directory to store data during the training")
    parser.add_argument(
        "-log_outdir", type=str, required=False, default=None,
        help="Directory to write logfiles to (defaults to run_outputs_dir)")
    parser.add_argument(
        "-model_save_dir", type=str, required=True,
        help='Path to a directory to store final model files')
    parser.add_argument(
        "-model", type=str, required=True,
        help="The model architecture to use for training\
             (see config/models.yaml)")
    parser.add_argument(
        "-labels", nargs='+', type=str, required=True,
        help='The labels to model')
    parser.add_argument(
        "-labels_loss_weights", nargs='+', type=float, default=None,
        help='A list of length labels indicating weights for the different\
              labels applied during model training')
    parser.add_argument(
        "-batch_size", type=int, default=128,
        help="The batch size for model training, if too large the model may\
              crash with an OOM error. Use values between 64 and 256")
    parser.add_argument(
        "-n_cpus", type=int, default=4,
        help="The number of cpus to use. Use all available if possible.")
    parser.add_argument(
        "-n_gpus", type=int, default=1,
        help='The number of GPUs to use (default 1)')
    parser.add_argument(
        "-buffer_size", type=int, default=32768,
        help='The buffer size to use for shuffling training records. Use \
              smaller values if memory is limited.')
    parser.add_argument(
        "-n_parallel_file_reads", type=int, default=50,
        help='How many files to read in parallel when counting the number of \
              records in tfr files.')
    parser.add_argument(
        "-max_epochs", type=int, default=70,
        help="The max number of epochs to train the model")
    parser.add_argument(
        "-starting_epoch", type=int, default=0,
        help="The starting epoch number (0-based index).")
    ######################################################################
    # Model Training Parameters
    ######################################################################
    parser.add_argument(
        "-initial_learning_rate", type=float, default=0.01,
        help="The initial learning rate.")
    parser.add_argument(
        "-optimizer", type=str, default="sgd",
        choices=['sgd', 'rmsprop'],
        required=False,
        help="Which optimizer to use in training the model.")
    parser.add_argument(
        "-early_stopping_patience", type=int, default=3,
        help="Number of epochs after which to stop training if no improvement \
              on validation set was observed (total loss).")
    parser.add_argument(
        "-reduce_lr_on_plateau_patience", type=int, default=2,
        help="Number of epochs after which to reduce learning rate if no \
              improvement on validation set was observed (total loss).")
    ######################################################################
    # Transfer-Learning and Model Loading
    ######################################################################
    parser.add_argument(
        "-transfer_learning", default=False,
        action='store_true', required=False,
        help="Option to specify that transfer learning should be used.")
    parser.add_argument(
        "-transfer_learning_type", default='last_layer', required=False,
        choices=['last_layer', 'all_layers'],
        help="Option to specify which transfer-learning stype hould be used: \
              'last_layer': allowing to adapt only the last layer,  \
              'all_layers': adapt all layers - default is 'last_layer'")
    parser.add_argument(
        "-continue_training", default=False,
        action='store_true', required=False,
        help="Flag that training should be continued from a saved model.")
    parser.add_argument(
        "-rebuild_model", default=False,
        action='store_true', required=False,
        help="Flag that model should be rebuild (if continue_training). \
              This might be necessary if model training should be continued\
              with different options (e.g. no GPUs, or different optimizer)")
    parser.add_argument(
        "-model_to_load", type=str, required=False, default=None,
        help='Path to a model (.hdf5) when either continue_training,\
             or transfer_learning are specified, \
             if a directory is specified, \
             the most recent model in that directory is loaded')
    ######################################################################
    # Image Processing
    ######################################################################
    parser.add_argument(
        "-color_augmentation", type=str, default=None,
        choices=[None, 'little', 'full_fast', 'full_randomized'],
        required=False,
        help="Which (random) color augmentation to perform during model\
              training - choose one of:\
              [None, 'little', 'full_fast', 'full_randomized']. \
              This can slow down the pre-processing speed and starve the \
              GPU of data. Use None or little/full_fast options if input \
              pipeline is slow. Generally full_randomized is recommended \
              and is usually more than fast enough.")
    parser.add_argument(
        "-preserve_aspect_ratio", action='store_true', default=None,
        dest='preserve_aspect_ratio',
        help="Wheter to preserve the aspect ratio of the images during model \
              training. This keeps the aspect ratio intact which may improve \
              model performance, however, may lead to cut-off areas at the \
              border of an image during training and prediction. If objects \
              of interest may occur at the edges we don't recommend to \
              specify this.")
    parser.add_argument(
        "-ignore_aspect_ratio", action='store_false', default=None,
        dest='preserve_aspect_ratio',
        help="Wheter to ignore the aspect ratio of the images during model \
              training.")
    parser.add_argument(
        "-randomly_flip_horizontally", dest='randomly_flip_horizontally',
        action='store_true', default=None,
        help="Whether to randomly flip the image during model training. \
              This almost always makes sense unless the training labels \
              are not invariant to flipping.")
    parser.add_argument(
        "-dont_randomly_flip_horizontally", dest='randomly_flip_horizontally',
        action='store_false', default=None,
        help="Whether to not randomly flip the image during model training. \
              This only makes sense if the training labels \
              are not invariant to flipping.")
    parser.add_argument(
        "-crop_factor", type=float, default=None,
        metavar="[0-0.5]",
        help="Wheter to randomly crop the image within \
             image-size * [1-crop_factor, 1] during model training. \
             This extracts a random portion of the image. \
             Values between 0 and 0.5 are allowed.")
    parser.add_argument(
        "-zoom_factor", type=float, default=None,
        metavar="[0-0.5]",
        help="Wheter to randomly zoom the image by: \
             image-size * [1 -zoom_factor, 1+zoom_factor] during model \
             training. Values between 0 and 0.5 are allowed.")
    parser.add_argument(
        "-rotate_by_angle", type=int, default=None,
        choices=range(0, 181),
        metavar="[0-180]",
        help="Wheter to randomly rotate the image during model training in \
              range [0 - rotate_by_angle, 0 + rotate_by_angle] \
             training. Values between 0 and 180 degrees are allowed.")
    parser.add_argument(
        "-image_choice_for_sets", type=str, default=None,
        choices=['random', 'grayscale_stacking'],
        help="How to choose an image for records with multiple images. \
              Default is 'random' which randomly chooses an image \
              during model training. 'grayscale_stacking' converts multiple \
              images into a single RGB image by blurring and converting \
              individual images to grayscale. Note that grayscale_stacking is \
              an experimental feature and is not yet supported when using \
              the predictor on new images. ")
    parser.add_argument(
        "-output_width", type=int, default=None,
        help="The output width in pixels of the image after pre-processing, \
              thus the input width of the image into the model. \
              Use this to override the default model-specific values as \
              specified in the config.yaml")
    parser.add_argument(
        "-output_height", type=int, default=None,
        help="The output height in pixels of the image after pre-processing, \
              thus the input height of the image into the model. \
              Use this to override the default model-specific values as \
              specified in the config.yaml")

    # Parse command line arguments
    args = vars(parser.parse_args())

    # Configure Logging
    if args['log_outdir'] is None:
        args['log_outdir'] = args['run_outputs_dir']

    setup_logging(log_output_path=args['log_outdir'])

    logger = logging.getLogger(__name__)

    logger.info("Using arguments:")
    for k, v in args.items():
        logger.info("Arg: %s: %s" % (k, v))

    ###########################################
    # Process Input ###########
    ###########################################

    # Load config file
    cfg_path = os.path.join(
        os.path.abspath(os.path.dirname(__file__)), 'config', 'config.yaml')

    config = ConfigLoader(cfg_path)

    assert args['model'] in config.cfg['models'], \
        "model %s not found in config/models.yaml" % args['model']

    # get default image_processing options from config
    image_processing = config.cfg['image_processing']

    # load model specific image processing parameters
    image_processing_model = \
        config.cfg['models'][args['model']]['image_processing']

    # overwrite parameters if specified by user
    to_overwrite = ['color_augmentation', 'preserve_aspect_ratio',
                    'crop_factor', 'zoom_factor', 'rotate_by_angle',
                    'randomly_flip_horizontally', 'image_choice_for_sets',
                    'output_width', 'output_height']
    for overwrite in to_overwrite:
        if args[overwrite] is not None:
            image_processing[overwrite] = args[overwrite]

    image_processing = {**image_processing_model, **image_processing}

    # disable color_augmentation for grayscale_stacking
    if image_processing['image_choice_for_sets'] == 'grayscale_stacking':
        if image_processing['color_augmentation'] is not None:
            image_processing['color_augmentation'] = None
            msg = "Disabling color_augmentation because of \
                   incompatibility with grayscale_stacking"
            logger.info(textwrap.shorten(msg, width=99))

    input_shape = (image_processing['output_height'],
                   image_processing['output_width'], 3)

    # Add 'label/' prefix to labels as they are stored in the .tfrecord files
    output_labels = args['labels']
    output_labels_clean = ['label/' + x for x in output_labels]

    # Class to numeric mappings and number of classes per label
    class_mapping = read_json(args['class_mapping_json'])
    # TODO: fix num classes per label for a:0, b:0 cases
    n_classes_per_label_dict = {c: len(set(class_mapping[o].values()))
                                for o, c in
                                zip(output_labels, output_labels_clean)}
    n_classes_per_label = [n_classes_per_label_dict[x]
                           for x in output_labels_clean]

    # save class mapping file to current run path
    export_dict_to_json(
        class_mapping,
        os.path.join(args['run_outputs_dir'], 'label_mappings.json'))

    # Find TFR files
    tfr_train = find_tfr_files_pattern_subdir(
        args['train_tfr_path'],
        args['train_tfr_pattern'])
    tfr_val = find_tfr_files_pattern_subdir(
        args['val_tfr_path'],
        args['val_tfr_pattern'])

    # Create best model output name
    best_model_save_path = os.path.join(args['model_save_dir'],
                                        'best_model.hdf5')

    # Define path of model to load if only directory is specified
    if args['model_to_load'] is not None:
        if not args['model_to_load'].endswith('.hdf5'):
            if os.path.isdir(args['model_to_load']):
                model_files = \
                    find_files_with_ending(args['model_to_load'], '.hdf5')
                most_recent_model = \
                    get_most_recent_file_from_files(model_files)
                args['model_to_load'] = most_recent_model
                logger.debug("Loading most recent model file %s:"
                             % most_recent_model)

    ###########################################
    # CALC IMAGE STATS ###########
    ###########################################

    logger.info("Start Calculating Image Stats")

    tfr_encoder_decoder = DefaultTFRecordEncoderDecoder()
    data_reader = DatasetReader(tfr_encoder_decoder.decode_record)

    # Calculate Dataset Image Means and Stdevs for a dummy batch
    logger.info("Get Dataset Reader for calculating dataset stats")
    n_records_train = n_records_in_tfr_dataset(
                        tfr_train,
                        n_parallel_file_reads=args['n_parallel_file_reads'])
    dataset = data_reader.get_iterator(
            tfr_files=tfr_train,
            batch_size=min([4096, n_records_train]),
            is_train=True,
            n_repeats=1,
            output_labels=output_labels,
            image_pre_processing_fun=preprocess_image,
            image_pre_processing_args={**image_processing,
                                       'is_training': False},
            buffer_size=args['buffer_size'],
            num_parallel_calls=args['n_cpus'])
    iterator = dataset.make_one_shot_iterator()
    batch_data = iterator.get_next()

    logger.info("Calculating image means and stdevs")
    with tf.Session() as sess:
        features, labels = sess.run(batch_data)

    # calculate and save image means and stdvs of each color channel
    # for pre processing purposes
    image_means = [round(float(x), 4) for x in
                   list(np.mean(features['images'],
                                axis=(0, 1, 2), dtype=np.float64))]
    image_stdevs = [round(float(x), 4) for x in
                    list(np.std(features['images'],
                                axis=(0, 1, 2), dtype=np.float64))]

    image_processing['image_means'] = image_means
    image_processing['image_stdevs'] = image_stdevs

    logger.info("Image Means: %s" % image_means)
    logger.info("Image Stdevs: %s" % image_stdevs)

    # Export Image Processing Settings
    export_dict_to_json({**image_processing,
                         'is_training': False},
                        os.path.join(args['run_outputs_dir'],
                                     'image_processing.json'))

    ###########################################
    # PREPARE DATA READER ###########
    ###########################################

    logger.info("Preparing Data Feeders")

    def input_feeder_train():
        return data_reader.get_iterator(
                    tfr_files=tfr_train,
                    batch_size=args['batch_size'],
                    is_train=True,
                    n_repeats=None,
                    output_labels=output_labels,
                    label_to_numeric_mapping=class_mapping,
                    image_pre_processing_fun=preprocess_image,
                    image_pre_processing_args={
                        **image_processing,
                        'is_training': True},
                    buffer_size=args['buffer_size'],
                    num_parallel_calls=args['n_cpus'])

    def input_feeder_val():
        return data_reader.get_iterator(
                    tfr_files=tfr_val,
                    batch_size=args['batch_size'],
                    is_train=False,
                    n_repeats=None,
                    output_labels=output_labels,
                    label_to_numeric_mapping=class_mapping,
                    image_pre_processing_fun=preprocess_image,
                    image_pre_processing_args={
                        **image_processing,
                        'is_training': False},
                    buffer_size=args['buffer_size'],
                    num_parallel_calls=args['n_cpus'])

    logger.info("Calculating batches per epoch")
    n_batches_per_epoch_train = calc_n_batches_per_epoch(
        n_records_train, args['batch_size'])

    n_records_val = n_records_in_tfr_dataset(
        tfr_val, n_parallel_file_reads=args['n_parallel_file_reads'])
    n_batches_per_epoch_val = calc_n_batches_per_epoch(
        n_records_val, args['batch_size'])

    logger.info("Found %s records in the training set" % n_records_train)
    logger.debug("Using %s batches/epoch for the training set" %
                 n_batches_per_epoch_train)
    logger.info("Found %s records in the validation set" % n_records_val)
    logger.debug("Using %s batches/epoch for the validation set" %
                 n_batches_per_epoch_val)

    ###########################################
    # CREATE MODEL ###########
    ###########################################

    logger.info("Preparing Model")

    model = create_model(
        model_name=args['model'],
        input_shape=input_shape,
        target_labels=output_labels_clean,
        n_classes_per_label_type=n_classes_per_label,
        n_gpus=args['n_gpus'],
        continue_training=args['continue_training'],
        rebuild_model=args['rebuild_model'],
        transfer_learning=args['transfer_learning'],
        transfer_learning_type=args['transfer_learning_type'],
        path_of_model_to_load=args['model_to_load'],
        initial_learning_rate=args['initial_learning_rate'],
        output_loss_weights=args['labels_loss_weights'])

    logger.debug("Final Model Architecture")
    for layer, i in zip(model.layers,
                        range(0, len(model.layers))):
        logger.debug("Layer %s: Name: %s Input: %s Output: %s" %
                     (i, layer.name, layer.input_shape,
                      layer.output_shape))

    ###########################################
    # MONITORS / HOOKS ###########
    ###########################################

    logger.info("Preparing Callbacks and Monitors")

    # stop model training if validation loss does not improve
    early_stopping = EarlyStopping(
        monitor='val_loss',
        min_delta=0,
        patience=args['early_stopping_patience'], verbose=0, mode='auto')

    # reduce learning rate if model progress plateaus
    reduce_lr_on_plateau = ReduceLROnPlateau(
        monitor='val_loss',
        factor=0.1,
        patience=args['reduce_lr_on_plateau_patience'],
        verbose=0,
        mode='auto',
        min_delta=0.0001, cooldown=1, min_lr=1e-5)

    # log validation statistics to a csv file
    csv_logger = CSVLogger(args['run_outputs_dir'] + 'training.log',
                           append=args['continue_training'])

    # create model checkpoints after each epoch
    checkpointer = ModelCheckpoint(
        filepath=args['run_outputs_dir'] +
        'model_epoch_{epoch:02d}_loss_{val_loss:.2f}.hdf5',
        monitor='val_loss', verbose=0, save_best_only=False,
        save_weights_only=False, mode='auto', period=1)

    # save best model
    checkpointer_best = ModelCheckpoint(
        filepath=args['run_outputs_dir'] + 'model_best.hdf5',
        monitor='val_loss', verbose=0, save_best_only=True,
        save_weights_only=False, mode='auto', period=1)

    # write graph to disk
    tensorboard = TensorBoard(log_dir=args['run_outputs_dir'],
                              histogram_freq=0,
                              batch_size=args['batch_size'],
                              write_graph=True,
                              write_grads=False, write_images=False)

    # Initialize tables (lookup tables)
    table_init = TableInitializerCallback()

    callbacks_list = [early_stopping, reduce_lr_on_plateau, csv_logger,
                      checkpointer, checkpointer_best, table_init, tensorboard]

    ###########################################
    # MODEL TRAINING  ###########
    ###########################################

    logger.info("Start Model Training")

    model.fit(
        input_feeder_train(),
        epochs=args['max_epochs'],
        steps_per_epoch=n_batches_per_epoch_train,
        validation_data=input_feeder_val(),
        validation_steps=n_batches_per_epoch_val,
        callbacks=callbacks_list,
        initial_epoch=args['starting_epoch'])

    logger.info("Finished Model Training")

    ###########################################
    # SAVE BEST MODEL ###########
    ###########################################

    logger.info("Saving Best Model")

    copy_models_and_config_files(
            model_source=args['run_outputs_dir'] + 'model_best.hdf5',
            model_target=best_model_save_path,
            files_path_source=args['run_outputs_dir'],
            files_path_target=args['model_save_dir'],
            copy_files=".json")

    ###########################################
    # PREDICT AND EXPORT TEST DATA ###########
    ###########################################

    if len(args['test_tfr_path']) > 0:

        logger.info("Starting to predict on test data")

        tfr_test = find_tfr_files_pattern_subdir(
            args['test_tfr_path'],
            args['test_tfr_pattern'])

        pred_output_json = os.path.join(args['run_outputs_dir'],
                                        'test_preds.json')

        tf.keras.backend.clear_session()

        tfr_encoder_decoder = DefaultTFRecordEncoderDecoder()
        logger.info("Create Dataset Reader")
        data_reader = DatasetReader(tfr_encoder_decoder.decode_record)

        def input_feeder_test():
            return data_reader.get_iterator(
                        tfr_files=tfr_test,
                        batch_size=args['batch_size'],
                        is_train=False,
                        n_repeats=1,
                        output_labels=output_labels,
                        image_pre_processing_fun=preprocess_image,
                        image_pre_processing_args={
                            **image_processing,
                            'is_training': False},
                        buffer_size=args['buffer_size'],
                        num_parallel_calls=args['n_cpus'],
                        drop_batch_remainder=False)

        pred = Predictor(
                model_path=best_model_save_path,
                class_mapping_json=args['class_mapping_json'],
                pre_processing_json=args['run_outputs_dir'] +
                'image_processing.json')

        pred.predict_from_dataset(
            dataset=input_feeder_test(),
            export_type='json',
            output_file=pred_output_json)

        logger.info("Finished predicting on test data, saved to: %s" %
                    pred_output_json)


if __name__ == '__main__':
    main()
