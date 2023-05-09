"""
Main code used to generate LC retention time predictions.

This provides the main interface. For the library versions see the .yml file
"""


__author__ = ["Robbin Bouwmeester", "Ralf Gabriels"]
__license__ = "Apache License, Version 2.0"
__maintainer__ = ["Robbin Bouwmeester", "Ralf Gabriels"]
__email__ = ["Robbin.Bouwmeester@ugent.be", "Ralf.Gabriels@ugent.be"]
__credits__ = [
    "Robbin Bouwmeester",
    "Ralf Gabriels",
    "Arthur Declercq"
    "Lennart Martens",
    "Sven Degroeve",
]


# Default models, will be used if no other is specified. If no best model is
# selected during calibration, the first model in the list will be used.
import os

deeplc_dir = os.path.dirname(os.path.realpath(__file__))
DEFAULT_MODELS = [
    "mods/full_hc_hela_hf_psms_aligned_1fd8363d9af9dcad3be7553c39396960.hdf5",
    "mods/full_hc_hela_hf_psms_aligned_8c22d89667368f2f02ad996469ba157e.hdf5",
    "mods/full_hc_hela_hf_psms_aligned_cb975cfdd4105f97efa0b3afffe075cc.hdf5",
    "mods/full_hc_PXD005573_mcp_cb975cfdd4105f97efa0b3afffe075cc.hdf5",
]
DEFAULT_MODELS = [os.path.join(deeplc_dir, dm) for dm in DEFAULT_MODELS]

LIBRARY = {}

import os
import sys
import copy
import gc
import logging
import multiprocessing
import multiprocessing.dummy
import pickle
import warnings
from configparser import ConfigParser
from tempfile import TemporaryDirectory
from copy import deepcopy
import random
import math

# If CLI/GUI/frozen: disable Tensorflow info and warnings before importing
IS_CLI_GUI = os.path.basename(sys.argv[0]) in ["deeplc", "deeplc-gui"]
IS_FROZEN = getattr(sys, "frozen", False)
if IS_CLI_GUI or IS_FROZEN:
    os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
    logging.getLogger("tensorflow").setLevel(logging.CRITICAL)
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    warnings.filterwarnings("ignore", category=FutureWarning)
    warnings.filterwarnings("ignore", category=UserWarning)


import numpy as np
import pandas as pd
import tensorflow as tf
from tensorflow.python.eager import context
from tensorflow.keras.models import load_model
import h5py

from deeplc._exceptions import CalibrationError, DeepLCError
from deeplc.trainl3 import train_en

from psm_utils.io.peptide_record import peprec_to_proforma
from psm_utils.psm import PSM
from psm_utils.psm_list import PSMList
from psm_utils.io import read_file
from psm_utils.io import write_file

from deeplcretrainer import deeplcretrainer

# "Custom" activation function
lrelu = lambda x: tf.keras.activations.relu(x, alpha=0.1, max_value=20.0)


try:
    from tensorflow.compat.v1.keras.backend import set_session
except ImportError:
    from tensorflow.keras.backend import set_session
try:
    from tensorflow.compat.v1.keras.backend import clear_session
except ImportError:
    from tensorflow.keras.backend import clear_session
try:
    from tensorflow.compat.v1.keras.backend import get_session
except ImportError:
    from tensorflow.keras.backend import get_session

# Set to force CPU calculations
os.environ["CUDA_VISIBLE_DEVICES"] = "-1"


# Set for TF V1.0 (counters some memory problems of nvidia 20 series GPUs)
# config = tf.ConfigProto()
# config.gpu_options.allow_growth = True
# session = tf.Session(config=config)

# Set for TF V2.0 (counters some memory problems of nvidia 20 series GPUs)
# config = tf.compat.v1.ConfigProto()
# config.gpu_options.allow_growth = True
# session = tf.compat.v1.Session(config=config)

# Feature extraction
from deeplc.feat_extractor import FeatExtractor
from pygam import LinearGAM, s

def warn(*args, **kwargs):
    pass
import warnings
warnings.warn = warn

# Supress warnings (or at least try...)
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3' 

warnings.filterwarnings('ignore', category=DeprecationWarning)
warnings.filterwarnings('ignore', category=FutureWarning)

logger = logging.getLogger(__name__)

def split_list(a, n):
    k, m = divmod(len(a), n)
    return (a[i*k+min(i, m):(i+1)*k+min(i+1, m)] for i in range(n))

def reset_keras():
    """Reset Keras session."""
    sess = get_session()
    clear_session()
    sess.close()
    gc.collect()
    # Set to force CPU calculations
    os.environ["CUDA_VISIBLE_DEVICES"] = "-1"


class DeepLC:
    """
    DeepLC predictor.

    Parameters
    ----------
    main_path : str
        main path of module
    path_model : str, optional
        path to prediction model(s); leave empty to select the best of the
        default models based on the calibration peptides
    verbose : bool, default=True
        turn logging on/off
    bin_dist : float, default=2
        TODO
    dict_cal_divider : int, default=50
        sets precision for fast-lookup of retention times for calibration; e.g.
        10 means a precision of 0.1 between the calibration anchor points
    split_cal : int, default=50
        number of splits in the chromatogram for piecewise linear calibration
        fit
    n_jobs : int, optional
        number of CPU threads to use
    config_file : str, optional
        path to configuration file
    f_extractor : object :: deeplc.FeatExtractor, optional
        deeplc.FeatExtractor object to use
    cnn_model : bool, default=True
        use CNN model or not
    batch_num : int, default=250000
        prediction batch size (in peptides); lower to decrease memory footprint
    write_library : bool, default=False
        append new predictions to library for faster future results; requires
        `use_library` option
    use_library : str, optional
        library file with previous predictions for faster results to read from,
        or to write to
    reload_library : bool, default=False
        reload prediction library

    Methods
    -------
    calibrate_preds(seqs=[], mods=[], identifiers=[], measured_tr=[], correction_factor=1.0, seq_df=None, use_median=True)
        Find best model and calibrate
    make_preds(seqs=[], mods=[], identifiers=[], calibrate=True, seq_df=None, correction_factor=1.0, mod_name=None)
        Make predictions

    """

    library = {}

    def __init__(
        self,
        main_path=os.path.dirname(os.path.realpath(__file__)),
        path_model=None,
        verbose=True,
        bin_dist=2,
        dict_cal_divider=50,
        split_cal=50,
        n_jobs=None,
        config_file=None,
        f_extractor=None,
        cnn_model=True,
        batch_num=250000,
        write_library=False,
        use_library=None,
        reload_library=False,
        pygam_calibration=True,
        deepcallc_mod=False,
        deeplc_retrain=False,
    ):  
        # Force pygam calibration... TODO fix this
        pygam_calibration=True

        # if a config file is defined overwrite standard parameters
        if config_file:
            cparser = ConfigParser()
            cparser.read(config_file)
            dict_cal_divider = cparser.getint("DeepLC", "dict_cal_divider")
            split_cal = cparser.getint("DeepLC", "split_cal")
            n_jobs = cparser.getint("DeepLC", "n_jobs")

        self.main_path = main_path
        self.verbose = verbose
        self.bin_dist = bin_dist
        self.calibrate_dict = {}
        self.calibrate_min = float("inf")
        self.calibrate_max = 0
        self.cnn_model = cnn_model

        self.batch_num = batch_num
        self.dict_cal_divider = dict_cal_divider
        self.split_cal = split_cal
        self.n_jobs = n_jobs

        max_threads = multiprocessing.cpu_count()
        if not self.n_jobs:
            self.n_jobs = max_threads
        elif self.n_jobs > max_threads:
            self.n_jobs = max_threads

        self.use_library = use_library
        self.write_library = write_library

        self.reload_library = reload_library

        tf.config.threading.set_intra_op_parallelism_threads(n_jobs)

        if "NUMEXPR_MAX_THREADS" not in os.environ:
            os.environ["NUMEXPR_MAX_THREADS"] = str(n_jobs)

        if path_model:
            self.model = path_model
        else:
            self.model = DEFAULT_MODELS

        if f_extractor:
            self.f_extractor = f_extractor
        else:
            self.f_extractor = FeatExtractor()

        self.pygam_calibration = pygam_calibration
        self.deeplc_retrain = deeplc_retrain

        if self.pygam_calibration:
            from pygam import LinearGAM, s

        self.deepcallc_mod = deepcallc_mod

        if self.deepcallc_mod:
            self.write_library = False
            self.use_library = None
            self.reload_library = False

    def __str__(self):
        return """
  _____                  _      _____
 |  __ \                | |    / ____|
 | |  | | ___  ___ _ __ | |   | |
 | |  | |/ _ \/ _ \ '_ \| |   | |
 | |__| |  __/  __/ |_) | |___| |____
 |_____/ \___|\___| .__/|______\_____|
                  | |
                  |_|
              """

    def do_f_extraction(self, seqs, mods, identifiers, charges=[]):
        """
        Extract all features we can extract; without parallelization; use if you
        want to run feature extraction with a single core

        Parameters
        ----------
        seqs : list
            peptide sequence list; should correspond to mods and identifiers
        mods : list
            naming of the mods; should correspond to seqs and identifiers
        identifiers : list
            identifiers of the peptides; should correspond to seqs and mods

        Returns
        -------
        pd.DataFrame
            feature matrix
        """
        list_of_psms = []
        if len(charges) > 0:
            for seq,mod,ident in zip(seqs,mods,identifiers):
                list_of_psms.append(PSM(peptide=peprec_to_proforma(seq,mod),spectrum_id=ident))
        else:
            for seq,mod,ident,z in zip(seqs,mods,identifiers,charges):
                list_of_psms.append(PSM(peptide=peprec_to_proforma(seq,mod),spectrum_id=ident))

        psm_list = PSMList(psm_list=list_of_psms)

        return self.f_extractor.full_feat_extract(psm_list)

    def do_f_extraction_pd(self,
                           df_instances,
                           charges=[]):
        """
        Extract all features we can extract; without parallelization; use if
        you want to run feature extraction with a single thread; and use a
        defined dataframe

        Parameters
        ----------
        df_instances : object :: pd.DataFrame
            dataframe containing the sequences (column:seq), modifications
            (column:modifications) and naming (column:index)

        Returns
        -------
        pd.DataFrame
            feature matrix
        """

        list_of_psms = []
        if len(charges) == 0:
            for seq,mod,ident in zip(df_instances["seq"],df_instances["modifications"],df_instances.index):
                list_of_psms.append(PSM(peptide=peprec_to_proforma(seq,mod),spectrum_id=ident))
        else:
            for seq,mod,ident,z in zip(df_instances["seq"],df_instances["modifications"],df_instances.index,charges=df_instances["charges"]):
                list_of_psms.append(PSM(peptide=peprec_to_proforma(seq,mod),spectrum_id=ident))
        psm_list = PSMList(psm_list=list_of_psms)

        return self.f_extractor.full_feat_extract(psm_list)

    def do_f_extraction_pd_parallel(self, df_instances):
        """
        Extract all features we can extract; with parallelization; use if you
        want to run feature extraction with multiple threads; and use a defined
        dataframe

        Parameters
        ----------
        df_instances : object :: pd.DataFrame
            dataframe containing the sequences (column:seq), modifications
            (column:modifications) and naming (column:index)

        Returns
        -------
        pd.DataFrame
            feature matrix
        """
        #self.n_jobs = 1

        df_instances_split = np.array_split(df_instances, math.ceil(self.n_jobs/4.0))
        if multiprocessing.current_process().daemon:
            logger.warning(
                "DeepLC is running in a daemon process. Disabling multiprocessing as daemonic processes can't have children."
            )
            pool = multiprocessing.dummy.Pool(1)
        else:
            pool = multiprocessing.Pool(math.ceil(self.n_jobs/4.0))

        if self.n_jobs == 1:
            df = self.do_f_extraction_pd(df_instances)
        else:
            df = pd.concat(
                pool.map(
                    self.do_f_extraction_pd,
                    df_instances_split))
            pool.close()
            pool.join()
        return df

    def do_f_extraction_psm_list(
                        self,
                        psm_list
            ):
        """
        Extract all features we can extract; without parallelization; use if
        you want to run feature extraction with a single thread; and use a
        defined dataframe

        Parameters
        ----------
        df_instances : object :: pd.DataFrame
            dataframe containing the sequences (column:seq), modifications
            (column:modifications) and naming (column:index)

        Returns
        -------
        pd.DataFrame
            feature matrix
        """
        return self.f_extractor.full_feat_extract(psm_list)

    def do_f_extraction_psm_list_parallel(
                        self,
                        psm_list
            ):
        """
        Extract all features we can extract; without parallelization; use if
        you want to run feature extraction with a single thread; and use a
        defined dataframe

        Parameters
        ----------
        df_instances : object :: pd.DataFrame
            dataframe containing the sequences (column:seq), modifications
            (column:modifications) and naming (column:index)

        Returns
        -------
        pd.DataFrame
            feature matrix
        """
        self.n_jobs = 1
        logger.info("prepare feature extraction")
        if multiprocessing.current_process().daemon:
            logger.warning("DeepLC is running in a daemon process. Disabling multiprocessing as daemonic processes can't have children.")
            psm_list_split = split_list(psm_list, self.n_jobs)
            pool = multiprocessing.dummy.Pool(1)
        elif self.n_jobs > 1:
            psm_list_split = split_list(psm_list, self.n_jobs)
            pool = multiprocessing.Pool(self.n_jobs)

        if self.n_jobs == 1:
            logger.info("start feature extraction")
            all_feats = self.do_f_extraction_psm_list(psm_list)
            logger.info("got feature extraction results")
        else:
            logger.info("start feature extraction")
            all_feats_async = pool.map_async(
                    self.do_f_extraction_psm_list,
                    psm_list_split)

            logger.info("wait for feature extraction")
            all_feats_async.wait()
            logger.info("get feature extraction results")
            all_feats = pd.concat(all_feats_async.get())
            logger.info("got feature extraction results")

            pool.close()
            pool.join()

        return all_feats

    def calibration_core(self,uncal_preds,cal_dict,cal_min,cal_max):
        cal_preds = []
        if len(uncal_preds) == 0:
            return np.array(cal_preds)
        if self.pygam_calibration:
            cal_preds = cal_dict.predict(uncal_preds)
        else:
            for uncal_pred in uncal_preds:
                try:
                    slope, intercept = cal_dict[str(round(uncal_pred, self.bin_dist))]
                    cal_preds.append(slope * (uncal_pred) + intercept)
                except KeyError:
                    # outside of the prediction range ... use the last
                    # calibration curve
                    if uncal_pred <= cal_min:
                        slope, intercept = cal_dict[str(round(cal_min, self.bin_dist))]
                        cal_preds.append(slope * (uncal_pred) + intercept)
                    elif uncal_pred >= cal_max:
                        slope, intercept = cal_dict[str(round(cal_max, self.bin_dist))]
                        cal_preds.append(slope * (uncal_pred) + intercept)
                    else:
                        slope, intercept = cal_dict[str(round(cal_max, self.bin_dist))]
                        cal_preds.append(slope * (uncal_pred) + intercept)
        return np.array(cal_preds)

    def make_preds_core_library(self,
                                psm_list=[],
                                calibrate=True,
                                mod_name=None
                                ):
        ret_preds = []
        for psm in psm_list:
            ret_preds.append(LIBRARY[psm.peptidoform.proforma+"|"+mod_name])

        if calibrate:
            try:
                ret_preds = self.calibration_core(ret_preds,self.calibrate_dict[mod_name],self.calibrate_min[mod_name],self.calibrate_max[mod_name])
            except:
                ret_preds = self.calibration_core(ret_preds,self.calibrate_dict,self.calibrate_min,self.calibrate_max)
        
        return ret_preds

    def make_preds_core(self,
                        X=[], 
                        X_sum=[], 
                        X_global=[], 
                        X_hc=[],
                        psm_list=[],
                        calibrate=True,
                        mod_name=None
                        ):
        """
        Make predictions for sequences
        Parameters
        ----------
        seq_df : object :: pd.DataFrame
            dataframe containing the sequences (column:seq), modifications
            (column:modifications) and naming (column:index); will use parallel
            by default!
        seqs : list
            peptide sequence list; should correspond to mods and identifiers
        mods : list
            naming of the mods; should correspond to seqs and identifiers
        identifiers : list
            identifiers of the peptides; should correspond to seqs and mods
        calibrate : boolean
            calibrate predictions or just return the predictions
        correction_factor : float
            correction factor to apply to predictions
        mod_name : str or None
            specify a model to use instead of the model assigned originally to
            this instance of the object
        Returns
        -------
        np.array
            predictions
        """
        if calibrate:
            assert (
                self.calibrate_dict
            ), "DeepLC instance is not yet calibrated.\
                                        Calibrate before making predictions, or use calibrate=False"

        if len(X) == 0 and len(psm_list) > 0:
            if self.verbose:

                logger.debug("Extracting features for the CNN model ...")
            #X = self.do_f_extraction_psm_list(psm_list)
            X = self.do_f_extraction_psm_list_parallel(psm_list)

            X_sum = np.stack(X["matrix_sum"].values())
            X_global = np.concatenate((np.stack(X["matrix_all"].values()),
                                    np.stack(X["pos_matrix"].values())),
                                    axis=1)
            X_hc = np.stack(X["matrix_hc"].values())
            X = np.stack(X["matrix"].values())
        elif len(X) == 0 and len(psm_list) == 0:
            return []

        ret_preds = []

        mod = load_model(
                    mod_name,
                    custom_objects={'<lambda>': lrelu}
                )
        try:
            X
            ret_preds = mod.predict(
                [X, X_sum, X_global, X_hc], batch_size=5120).flatten()
        except UnboundLocalError:

            logger.debug("X is empty, skipping...")
            ret_preds = []

        if calibrate:
            try:
                ret_preds = self.calibration_core(ret_preds,self.calibrate_dict[mod_name],self.calibrate_min[mod_name],self.calibrate_max[mod_name])
            except:
                ret_preds = self.calibration_core(ret_preds,self.calibrate_dict,self.calibrate_min,self.calibrate_max)
        
        return ret_preds

    def make_preds(self,
                   psm_list=None,
                   infile="",
                   calibrate=True,
                   seq_df=None,
                   mod_name=None):
        """
        Make predictions for sequences, in batches if required.

        Parameters
        ----------
        seq_df : object :: pd.DataFrame
            dataframe containing the sequences (column:seq), modifications
            (column:modifications) and naming (column:index); will use parallel
            by default!
        seqs : list
            peptide sequence list; should correspond to mods and identifiers
        mods : list
            naming of the mods; should correspond to seqs and identifiers
        identifiers : list
            identifiers of the peptides; should correspond to seqs and mods
        calibrate : boolean
            calibrate predictions or just return the predictions
        correction_factor : float
            correction factor to apply to predictions
        mod_name : str or None
            specify a model to use instead of the model assigned originally to
            this instance of the object

        Returns
        -------
        np.array
            predictions
        """
        if type(seq_df) == pd.core.frame.DataFrame:
            list_of_psms = []
            for seq,mod,ident in zip(seq_df["seq"],seq_df["modifications"],seq_df.index):
                list_of_psms.append(PSM(peptidoform=peprec_to_proforma(seq,mod),spectrum_id=ident))
            psm_list = PSMList(psm_list=list_of_psms)
        
        if len(infile) > 0:
            psm_list = read_file(infile)
            if "msms" in infile and ".txt" in infile:
                mapper = pd.read_csv(os.path.join(os.path.dirname(os.path.realpath(__file__)), "unimod/map_mq_file.csv"),index_col=0)["value"].to_dict()
                psm_list.rename_modifications(mapper)

        if len(psm_list) > 0:
            if self.verbose:
                logger.debug("Extracting features for the CNN model ...")

            X = self.do_f_extraction_psm_list_parallel(psm_list)
            X_sum = np.stack(X["matrix_sum"].values())
            X_global = np.concatenate((np.stack(X["matrix_all"].values()),
                                    np.stack(X["pos_matrix"].values())),
                                    axis=1)
            X_hc = np.stack(X["matrix_hc"].values())
            X = np.stack(X["matrix"].values())
        else:
            return []

        ret_preds = []

        if isinstance(self.model, dict):
            for m_group_name,m_name in self.model.items():
                ret_preds.append(self.make_preds_core(X=X, 
                                    X_sum=X_sum, 
                                    X_global=X_global, 
                                    X_hc=X_hc,
                                    calibrate=calibrate,
                                    mod_name=m_name))
            ret_preds = np.array([sum(a)/len(a) for a in zip(*ret_preds)])
        elif mod_name is not None:
            ret_preds = self.make_preds_core(X=X, 
                                             X_sum=X_sum, 
                                             X_global=X_global, 
                                             X_hc=X_hc,
                                             calibrate=calibrate,
                                             mod_name=mod_name)
        elif isinstance(self.model, list):
            for m_name in self.model:
                ret_preds.append(self.make_preds_core(X=X, 
                                    X_sum=X_sum, 
                                    X_global=X_global, 
                                    X_hc=X_hc,
                                    calibrate=calibrate,
                                    mod_name=m_name))
            ret_preds = np.array([sum(a)/len(a) for a in zip(*ret_preds)])
        else:
            ret_preds = self.make_preds_core(X=X, 
                                             X_sum=X_sum, 
                                             X_global=X_global, 
                                             X_hc=X_hc,
                                             calibrate=calibrate,
                                             mod_name=self.model)
        return ret_preds
        # todo make this multithreaded

    def calibrate_preds_func_pygam(self,
                                   psm_list=None,
                                   correction_factor=1.0,
                                   seq_df=None,
                                   measured_tr=None,
                                   use_median=True,
                                   mod_name=None):
        # TODO make a df to psm_list function
        # TODO make sure either psm_list or seq_df is supplied
        if type(seq_df) == pd.core.frame.DataFrame:
            list_of_psms = []
            for seq,mod,ident,tr in zip(seq_df["seq"],seq_df["modifications"],seq_df.index,seq_df["tr"]):
                list_of_psms.append(PSM(peptidoform=peprec_to_proforma(seq,mod),spectrum_id=ident,retention_time=tr))
            psm_list = PSMList(psm_list=list_of_psms)

            measured_tr = [psm.retention_time for psm in psm_list]

        predicted_tr = self.make_preds(
            psm_list,
            calibrate=False,
            mod_name=mod_name)

        # sort two lists, predicted and observed based on measured tr
        tr_sort = [
            (mtr, ptr)
            for mtr, ptr in sorted(
                zip(measured_tr, predicted_tr), key=lambda pair: pair[1]
            )
        ]
        measured_tr = np.array([mtr for mtr, ptr in tr_sort], dtype=np.float32)
        predicted_tr = np.array([ptr for mtr, ptr in tr_sort], dtype=np.float32)

        # predicted_tr = list(predicted_tr)
        # measured_tr = list(measured_tr)

        gam_model_cv = LinearGAM(s(0), verbose=True).fit(predicted_tr, measured_tr)
        calibrate_min = min(predicted_tr)
        calibrate_max = max(predicted_tr)
        return calibrate_min, calibrate_max, gam_model_cv

    def calibrate_preds_func(self,
                             psm_list=None,
                             correction_factor=1.0,
                             seq_df=None,
                             use_median=True,
                             mod_name=None):
        """
        Make calibration curve for predictions

        Parameters
        ----------
        seqs : list
            peptide sequence list; should correspond to mods and identifiers
        mods : list
            naming of the mods; should correspond to seqs and identifiers
        identifiers : list
            identifiers of the peptides; should correspond to seqs and mods
        measured_tr : list
            measured tr of the peptides; should correspond to seqs, identifiers,
            and mods
        correction_factor : float
            correction factor that needs to be applied to the supplied measured
            trs
        seq_df : object :: pd.DataFrame
            a pd.DataFrame that contains the sequences, modifications and
            observed retention times to fit a calibration curve
        use_median : boolean
            flag to indicate we need to use the median valuein a window to
            perform calibration
        mod_name
            specify a model to use instead of the model assigned originally to
            this instance of the object

        Returns
        -------
        float
            the minimum value where a calibration curve was fitted, lower values
            will be extrapolated from the minimum fit of the calibration curve
        float
            the maximum value where a calibration curve was fitted, higher values
            will be extrapolated from the maximum fit of the calibration curve
        dict
            dictionary with keys for rounded tr, and the values concern a linear
            model that should be applied to do calibration (!!! what is the
            shape of this?)
        """
        if type(seq_df) == pd.core.frame.DataFrame:
            list_of_psms = []
            for seq,mod,ident in zip(seq_df["seq"],seq_df["modifications"],seq_df.index):
                list_of_psms.append(PSM(peptidoform=peprec_to_proforma(seq,mod),spectrum_id=ident))
            psm_list = PSMList(psm_list=list_of_psms)

        predicted_tr = self.make_preds(
            psm_list,
            calibrate=False,
            mod_name=mod_name)
        measured_tr = seq_df["tr"]

        # sort two lists, predicted and observed based on measured tr
        tr_sort = [
            (mtr, ptr)
            for mtr, ptr in sorted(
                zip(measured_tr, predicted_tr), key=lambda pair: pair[1]
            )
        ]
        measured_tr = np.array([mtr for mtr, ptr in tr_sort])
        predicted_tr = np.array([ptr for mtr, ptr in tr_sort])

        mtr_mean = []
        ptr_mean = []

        calibrate_dict = {}
        calibrate_min = float("inf")
        calibrate_max = 0

        if self.verbose:
            logger.debug(
                "Selecting the data points for calibration (used to fit the linear models between)"
            )

        # smooth between observed and predicted
        split_val = predicted_tr[-1] / self.split_cal
        for range_calib_number in np.arange(0.0, predicted_tr[-1], split_val):
            ptr_index_start = np.argmax(predicted_tr >= range_calib_number)
            ptr_index_end = np.argmax(predicted_tr >= range_calib_number + split_val)

            # no points so no cigar... use previous points
            if ptr_index_start >= ptr_index_end:
                logger.debug(
                    "Skipping calibration step, due to no points in the "
                    "predicted range (are you sure about the split size?): "
                    "%s,%s",
                    range_calib_number,
                    range_calib_number + split_val,
                )
                continue

            mtr = measured_tr[ptr_index_start:ptr_index_end]
            ptr = predicted_tr[ptr_index_start:ptr_index_end]

            if use_median:
                mtr_mean.append(np.median(mtr))
                ptr_mean.append(np.median(ptr))
            else:
                mtr_mean.append(sum(mtr) / len(mtr))
                ptr_mean.append(sum(ptr) / len(ptr))

        if self.verbose:
            logger.debug("Fitting the linear models between the points")

        if self.split_cal >= len(measured_tr):
            raise CalibrationError(
                "Not enough measured tr ({}) for the chosen number of splits ({}). "
                "Choose a smaller split_cal parameter or provide more peptides for "
                "fitting the calibration curve.".format(
                    len(measured_tr), self.split_cal
                )
            )
        if len(mtr_mean) == 0:
            raise CalibrationError(
                "The measured tr list is empty, not able to calibrate"
            )
        if len(ptr_mean) == 0:
            raise CalibrationError(
                "The predicted tr list is empty, not able to calibrate"
            )

        # calculate calibration curves
        for i in range(0, len(ptr_mean)):
            if i >= len(ptr_mean) - 1:
                continue
            delta_ptr = ptr_mean[i + 1] - ptr_mean[i]
            delta_mtr = mtr_mean[i + 1] - mtr_mean[i]

            slope = delta_mtr / delta_ptr
            intercept = (-1 * (ptr_mean[i] * slope)) + mtr_mean[i]

            # optimized predictions using a dict to find calibration curve very
            # fast
            for v in np.arange(
                round(ptr_mean[i], self.bin_dist),
                round(ptr_mean[i + 1], self.bin_dist),
                1 / ((self.bin_dist) * self.dict_cal_divider),
            ):
                if v < calibrate_min:
                    calibrate_min = v
                if v > calibrate_max:
                    calibrate_max = v
                calibrate_dict[str(round(v, self.bin_dist))] = [slope, intercept]

        return calibrate_min, calibrate_max, calibrate_dict

    def calibrate_preds(self,
                        psm_list=None,
                        infile="",
                        measured_tr=[],
                        correction_factor=1.0,
                        location_peprec_retraining="",
                        location_retraining_models="",
                        psm_utils_obj=None,
                        sample_for_calibration_curve=None,
                        seq_df=None,
                        use_median=True):
        """
        Find best model and calibrate.

        Parameters
        ----------
        seqs : list
            peptide sequence list; should correspond to mods and identifiers
        mods : list
            naming of the mods; should correspond to seqs and identifiers
        identifiers : list
            identifiers of the peptides; should correspond to seqs and mods
        measured_tr : list
            measured tr of the peptides; should correspond to seqs, identifiers,
            and mods
        correction_factor : float
            correction factor that needs to be applied to the supplied measured
            trs
        seq_df : object :: pd.DataFrame
            a pd.DataFrame that contains the sequences, modifications and
            observed retention times to fit a calibration curve
        use_median : boolean
            flag to indicate we need to use the median valuein a window to
            perform calibration

        Returns
        -------

        """
        if type(seq_df) == pd.core.frame.DataFrame:
            list_of_psms = []
            for seq,mod,ident,tr in zip(seq_df["seq"],seq_df["modifications"],seq_df.index,seq_df["tr"]):
                list_of_psms.append(PSM(peptidoform=peprec_to_proforma(seq,mod),spectrum_id=ident,retention_time=tr))
            psm_list = PSMList(psm_list=list_of_psms)
            

        if isinstance(self.model, str):
            self.model = [self.model]
        
        if len(infile) > 0:
            psm_list = read_file(infile)
            if "msms" in infile and ".txt" in infile:
                mapper = pd.read_csv(os.path.join(os.path.dirname(os.path.realpath(__file__)), "unimod/map_mq_file.csv"),index_col=0)["value"].to_dict()
                psm_list.rename_modifications(mapper)

        measured_tr = [psm.retention_time for psm in psm_list]

        if self.verbose:
            logger.debug("Start to calibrate predictions ...")
        if self.verbose:

            logger.debug(
                "Ready to find the best model out of: %s" %
                (self.model))
        
        best_perf = float("inf")
        best_calibrate_min = 0.0
        best_calibrate_max = 0.0
        best_calibrate_dict = {}
        mod_calibrate_dict = {}
        mod_calibrate_min_dict = {}
        mod_calibrate_max_dict = {}
        pred_dict = {}
        mod_dict = {}

        if self.deeplc_retrain:
            # The following code is not required in most cases, but here it is used to clear variables that might cause problems
            _ = tf.Variable([1])

            context._context = None
            context._create_context()

            tf.config.threading.set_inter_op_parallelism_threads(1)

            if len(location_retraining_models) > 0:
                t_dir_models = TemporaryDirectory().name
                os.mkdir(t_dir_models)
            else:
                t_dir_models = location_retraining_models
                try:
                    os.mkdir(t_dir_models)
                except:
                    pass

            # Here we will apply transfer learning we specify previously trained models in the 'mods_transfer_learning'
            models = deeplcretrainer.retrain(
                {"deeplc_transferlearn":psm_list},
                outpath=t_dir_models,
                mods_transfer_learning=self.model,
                freeze_layers=True,
                n_epochs=10,
                freeze_after_concat=1,
            )

            self.model = models

        if isinstance(sample_for_calibration_curve, int):
            psm_list = random.sample(list(psm_list), sample_for_calibration_curve)
            measured_tr = [psm.retention_time for psm in psm_list]

        for m in self.model:
            if self.verbose:
                logger.debug("Trying out the following model: %s" % (m))
            if self.pygam_calibration:
                calibrate_output = self.calibrate_preds_func_pygam(
                    psm_list,
                    measured_tr=measured_tr,
                    correction_factor=correction_factor,
                    seq_df=seq_df,
                    use_median=use_median,
                    mod_name=m,
                )
            else:
                calibrate_output = self.calibrate_preds_func(
                    psm_list,
                    measured_tr=measured_tr,
                    correction_factor=correction_factor,
                    seq_df=seq_df,
                    use_median=use_median,
                    mod_name=m,
                )

            (
                self.calibrate_min,
                self.calibrate_max,
                self.calibrate_dict,
            ) = calibrate_output

            if type(self.calibrate_dict) == dict:
                if len(self.calibrate_dict.keys()) == 0:
                    continue
            
            m_name = m.split("/")[-1]

            preds = self.make_preds(psm_list,
                                    calibrate=True,
                                    seq_df=seq_df,
                                    mod_name=m)

            if self.deepcallc_mod:
                m_group_name = "deepcallc"
            else:
                m_group_name = "_".join(m_name.split("_")[:-1])

            try:
                pred_dict[m_group_name][m] = preds
                mod_dict[m_group_name][m] = m
                mod_calibrate_dict[m_group_name][m] = self.calibrate_dict
                mod_calibrate_min_dict[m_group_name][m] = self.calibrate_min
                mod_calibrate_max_dict[m_group_name][m] = self.calibrate_max
            except:
                pred_dict[m_group_name] = {}
                mod_dict[m_group_name] = {}
                mod_calibrate_dict[m_group_name] = {}
                mod_calibrate_min_dict[m_group_name] = {}
                mod_calibrate_max_dict[m_group_name] = {}

                pred_dict[m_group_name][m] = preds
                mod_dict[m_group_name][m] = m
                mod_calibrate_dict[m_group_name][m] = self.calibrate_dict
                mod_calibrate_min_dict[m_group_name][m] = self.calibrate_min
                mod_calibrate_max_dict[m_group_name][m] = self.calibrate_max

        for m_name in pred_dict.keys():
            preds = [sum(a) / len(a) for a in zip(*list(pred_dict[m_name].values()))]
            if len(measured_tr) == 0:
                perf = sum(abs(seq_df["tr"] - preds))
            else:
                perf = sum(abs(np.array(measured_tr) - np.array(preds)))

            if self.verbose:
                logger.debug(
                    "For %s model got a performance of: %s"
                    % (m_name, perf / len(preds))
                )

            if perf < best_perf:
                if self.deepcallc_mod:
                    m_group_name = "deepcallc"
                else:
                    m_group_name = m_name
                    # TODO is deepcopy really required?

                best_calibrate_dict = copy.deepcopy(mod_calibrate_dict[m_group_name])
                best_calibrate_min = copy.deepcopy(mod_calibrate_min_dict[m_group_name])
                best_calibrate_max = copy.deepcopy(mod_calibrate_max_dict[m_group_name])

                best_model = copy.deepcopy(mod_dict[m_group_name])
                best_perf = perf

        self.calibrate_dict = best_calibrate_dict
        self.calibrate_min = best_calibrate_min
        self.calibrate_max = best_calibrate_max
        self.model = best_model

        if self.deepcallc_mod:
            self.deepcallc_model = train_en(pd.DataFrame(pred_dict["deepcallc"]),seq_df["tr"])

        self.n_jobs = 1

        logger.debug("Model with the best performance got selected: %s" % (best_model))

    def split_seq(self, a, n):
        """
        Split a list (a) into multiple chunks (n)

        Parameters
        ----------
        a : list
            list to split
        n : list
            number of chunks

        Returns
        -------
        list
            chunked list
        """

        # since chunking is not alway possible do the modulo of residues
        k, m = divmod(len(a), n)
        result = (a[i * k + min(i, m) : (i + 1) * k + min(i + 1, m)] for i in range(n))
        return result
