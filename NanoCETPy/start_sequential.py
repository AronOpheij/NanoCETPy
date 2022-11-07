# -*- coding: utf-8 -*-
"""
    .. todo::
        Save diagnostic images in the h5 datafile:

        #. After manual alignment, an image from the "bottom-camera" illuminated with the white TOP LED.
        #. After the initial alignment procedure, an image from the "top-camera" of the core of the fiber (with
        the red LED).
        #. Preferably after the final alignment procedure, an image with the "bottom-camera", of the laser
        scattered background.

        For the moment, add these to each h5 file measured after this alignment.

    .. todo:
        The filename displayed in the GUI is incorrect. It should not be grabbed from disk, but generated by
        experiment.
    .. todo:
        (low priority) Sometimes there are json related python errors when closing the measurement.
        These don't appear to cause actual trouble though.

    :copyright: 2022 by NanoCETPy Authors. See AUTHORS for full list
    :LICENSE: GPLv3. See LICENSE for more information
"""
import os
import sys

import yaml
from PyQt5 import QtGui
from PyQt5.QtWidgets import QApplication

from experimentor.lib.log import get_logger, log_to_screen
from NanoCETPy.sequential.models.demo import DemoExperiment
from NanoCETPy.sequential.models.experiment import MainSetup
from NanoCETPy.sequential.views.sequential_window import SequentialMainWindow

from NanoCETPy import BASE_PATH



def main():
    logger = get_logger()
    log_to_screen(logger=logger)
    if len(sys.argv) > 1 and sys.argv[1] == 'demo':
        experiment = DemoExperiment()
    else:
        experiment = MainSetup()
    if not (config_filepath := BASE_PATH / 'config_user.yml').is_file():
        config_filepath = BASE_PATH / 'resources/config_default.yml'
    experiment.load_configuration(config_filepath, yaml.UnsafeLoader)

    # QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    # QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "1"
    app = QApplication([])
    fontId = QtGui.QFontDatabase.addApplicationFont(str(BASE_PATH / 'resources' / 'Roboto-Regular.ttf'))
    families = QtGui.QFontDatabase.applicationFontFamilies(fontId)
    font = QtGui.QFont(families[0])
    app.setFont(font)
    main_window = SequentialMainWindow(experiment=experiment)
    main_window.show()
    app.exec()
    experiment.finalize()
    return experiment


if __name__ == '__main__':
    debug = main()
