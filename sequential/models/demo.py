import numpy as np
import time
from skimage import io
import os
import yaml
BASE_DIR_VIEW = os.path.dirname(os.path.abspath(__file__))

from experimentor.models.action import Action
from experimentor.models.experiments import Experiment



class DemoExperiment(Experiment):

    def __init__(self, filename=None):
        super(DemoExperiment, self).__init__(filename=filename)
        self.camera_fiber = DemoCam()
        self.camera_microscope = DemoCam()
        self.electronics = DemoElectronics()
        self.aligned = True
        self.active = True
        self.saving = True
        self.microscope_image = io.imread(os.path.join(BASE_DIR_VIEW, 'mic_demo.png'), as_gray=True)
        self.waterfall_image = io.imread(os.path.join(BASE_DIR_VIEW, 'wat_demo.png'), as_gray=True)
        self.logger.info(f'SHAPES {self.microscope_image.shape}, {self.waterfall_image.shape}')
        pass

    def toggle_active(self):
        pass

    @Action
    def initialize(self):
        time.sleep(2)
        pass

    def focus_start(self):
        pass

    def get_latest_image(self):
        return self.microscope_image.T

    def get_waterfall_image(self):
        return self.waterfall_image.T

    def focus_stop(self):
        pass

    @Action
    def start_alignment(self):
        time.sleep(1)
        self.aligned = True
        pass

    def find_ROI(self):
        pass

    def update_camera(self, *args):
        pass
    
    @Action
    def save_waterfall(self):
        self.saving=True
        while self.active:
            time.sleep(.1)
        self.saving=False
        pass

    def finalize(self):
        with open('config_user.yml', 'w') as f:
            yaml.dump(self.config, f, default_flow_style=False)

class DemoCam:
    def __init__(self):
        self.continuous_reads_running = True
        self.initialized = True
        self.camera = 'DemoCam'

class DemoElectronics:
    def __init__(self):
        self.initialized = True