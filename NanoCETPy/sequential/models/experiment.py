"""
    Experiment module for the entire usage sequence of the NanoCET 
"""
# Is this is True, the focus and alignment algorithms will effectively be skipped:
SKIP_ALIGNING = False  # Default: False
# String pointing to h5 file to use as test data. Replace with something that returns False to skip
USE_TEST_DATA = r'C:\Users\aron\NanoCET\2022-09-22\Waterfall_70nm_2.h5'  # Default: ''
SKIP_FIRST_LINES_IN_TEST_DATA = 500

# USE_TEST_DATA = r'C:\Users\aron\NanoCET\2022-09-22\Waterfall_70nm_3.h5'  # Default: ''
# SKIP_FIRST_LINES_IN_TEST_DATA = 0
USE_TEST_DATA = False

import os
import time
from datetime import datetime
from multiprocessing import Event

import numpy as np
import yaml
from scipy import optimize, ndimage
from skimage import data

from experimentor import Q_
from experimentor.models.action import Action
from experimentor.models.decorators import make_async_thread
from experimentor.models.experiments import Experiment
from NanoCETPy.sequential.models import model_utils as ut
from NanoCETPy.sequential.models.arduino import ArduinoNanoCET
from NanoCETPy.sequential.models.basler import BaslerNanoCET as Camera
from NanoCETPy.sequential.models.movie_saver import WaterfallSaver


class MainSetup(Experiment):
    """ This is a Experiment subclass to control the NanoCET in a sequential experiment consisting of focusing, alignment, and recording a waterfall

    :param str filename: yml file containing the configuration settings for the experiment

    >>> # First the experiment is instantiated with the corresponding filename
    >>> experiment = MainSetup('config.yml')
    >>> # Or 
    >>> experiment = MainSetup()
    >>> experiment.load_configuration('config.yml', yaml.UnsafeLoader)

    >>> # Then the experiment is initialized to load the connected device classes
    >>> experiment.initialize()
    """
    def __init__(self, filename=None):
        super(MainSetup, self).__init__(filename=filename)
        self.VERSION = '1.0'
        self.camera_fiber = None
        self.camera_microscope = None
        self.electronics = None
        self.display_camera = self.camera_microscope
        self.finalized = False
        self.saving_event = Event()
        self.saving = False
        self.saving_process = None
        self.aligned = False
        
        self.demo_image = data.colorwheel()
        self.waterfall_image = np.array([[0,2**12-1],[0,2**8-1]])
        self.waterfall_image = np.zeros((2,2))
        self.waterfall_image_limits = [0, 1]
        self.display_image = self.demo_image
        self.active = True
        self.now = None
        self._trigger_camera_auto_range = False

    @Action
    def initialize(self):
        """ Initializes the cameras and Arduino objects. 
        Runs in a loop until every device is connected and initialized.

        :return: None
        """
        #self.initialize_cameras()
        #self.initialize_electronics()

        #Instantiate Camera and Arduino objects
        self.logger.info('Instantiating Cameras and Arduino')
        config_fiber = self.config['camera_fiber']
        self.camera_fiber = Camera(config_fiber['init'], initial_config=config_fiber['config'])
        config_mic = self.config['camera_microscope']
        self.camera_microscope = Camera(config_mic['init'], initial_config=config_mic['config'])
        self.electronics = ArduinoNanoCET(**self.config['electronics']['arduino'])
        try:
            devices_loading_timeout = Q_(self.config['defaults']['devices_loading_timeout']).m_as('s')
        except:
            devices_loading_timeout = 30
            self.logger.info(f'default/devices_loading_timeout parameter not found in config: using {devices_loading_timeout}s')

        #Loop over instances until all are initialized
        t0 = time.time()
        loading_timed_out = False
        while self.active and not loading_timed_out:
            #self.logger.info('TEST init loop')
            initialized = [self.camera_fiber.initialized, self.camera_microscope.initialized, self.electronics.initialized]
            if all(initialized): return
            if not initialized[0]: 
                try:
                    self.camera_fiber.initialize()
                except:
                    self.logger.info('Init Exception camera_fiber:', exc_info=True)
            if not initialized[1]: 
                try:
                    self.camera_microscope.initialize()
                except:
                    self.logger.info('Init Exception camera_microscope:', exc_info=True)
            if not initialized[2]:
                try:
                    if not self.electronics.initializing: self.electronics.initialize()
                except:
                    self.electronics.initializing = False 
                    self.logger.info('Init Exception electronics:', exc_info=True)
            loading_timed_out = time.time() > t0 + devices_loading_timeout
        if loading_timed_out:
            self.logger.error('Loading devices timed out')
            self.parent.init_failed.emit()
            return
        self.logger.info('TEST init loop exit')


    def focus_start(self):
        """
        Live view for manual focussing of microscope.
        """
        self.electronics.scattering_laser = 0
        self.electronics.top_led = 1
        self.set_live(self.camera_microscope, False)
        self.set_live(self.camera_fiber, False)
        while self.camera_microscope.free_run_running: time.sleep(.1)
        time.sleep(.1)
        # self.camera_fiber.clear_ROI()
        # self.camera_microscope.clear_ROI()
        self.camera_fiber.ROI = self.config['camera_fiber']['config']['ROI']
        self.camera_microscope.ROI = self.config['camera_microscope']['config']['ROI']
        self.set_live(self.camera_microscope, True)
        self.update_camera(self.camera_microscope, self.config['defaults']['microscope_focusing']['low'])
        self.aligned = False

    def focus_stop(self):
        self.set_live(self.camera_microscope, False)
        self.electronics.top_led = 0
        self.img_focus_microscope = self.camera_microscope.temp_image.copy()


    def identify_fiber_core_in_microscope(self, img):
        """
        Tries to find the fiber core in a microscope image.
        If it fails, it will return a value near the middle.

        returns (int) center pixel index
        """
        cross_cut = np.median(img, axis=0)
        cross_cut = np.convolve(cross_cut, [0.25, 0.5, 0.25], 'same')  # smooth a bit
        cross_cut[0], cross_cut[-1] = cross_cut[1], cross_cut[-2]  # fix endpoints
        cross_cut_diff = np.diff(cross_cut)
        side1 = np.argmin(cross_cut_diff)
        side2 = np.argmax(cross_cut_diff)
        self.logger.info(f'Sides at [{side1}, {side2}]')
        if np.abs((side2 - side1) - 415) < 15:
            center_estimate = (side1 + side2)/2  # The middle between the two edges
            self.logger.info(f'Full fiber found, center_estimate = {center_estimate}')
        elif np.abs((side2 - side1) - 207) < 10:
            # If the edge of the fiber is ont visible and the distance is ~ 207, the other min/max could be the core
            if cross_cut_diff.max() > -cross_cut_diff.min():
                center_estimate = side1 + 5
            else:
                center_estimate = side2 - 5
            self.logger.info(f'Partial fiber found, center_estimate = {center_estimate}')
        else:
            center_estimate = len(cross_cut) / 2
            self.logger.info(f"Couldn't find fiber, center_estimate = {center_estimate}")
        center_fit = int(center_estimate) - 9 + 3 + np.argmin(np.convolve(cross_cut[int(center_estimate) - 9: int(center_estimate) + 10], np.ones(7) / 7, 'valid'))
        self.logger.info(f'Center by local minimum = {center_fit}')
        center = np.ceil((center_estimate + center_fit * 2) / 3)
        self.logger.info(f'Core identified at {center}')
        return center

    @make_async_thread
    def start_alignment(self):
        """ Wraps the whole alignment procedure from focussing to aligning.
        Run in an async thread as it calls other Actions
        
        :return: None
        """
        self.logger.info('TEST Starting Laser Alignment')
        self.active = True
        self.now = datetime.now()
        self.saving_images = True
        self._trigger_camera_auto_range = False

        if False: # TESTING
            time.sleep(5)
            self.aligned = True
            self.toggle_live(self.camera_microscope)
            self.set_laser_power(99)
            self.update_camera(self.camera_microscope, self.config['defaults']['microscope_focusing']['high'])
            return

        # Set camera mode
        self.set_live(self.camera_fiber, False)
        self.set_live(self.camera_microscope, False)
        self.camera_fiber.acquisition_mode = self.camera_fiber.MODE_SINGLE_SHOT
        self.camera_microscope.acquisition_mode = self.camera_microscope.MODE_SINGLE_SHOT
        # Set exposure and gain

        # Turn on Laser
        # self.electronics.fiber_led = 0
        self.electronics.top_led = 0
        self.electronics.side_led = 0
        self.electronics.scattering_laser = 0

        # Step 1: Check if the (unfocused) fiber facet is the expected location. (using the fiber LED)
        if not self.validate_initial_fiber_position():
            self.logger.warning('Check cartridge position')
            self.aligned = 'check cartridge'  # This will signal to the GUI to show a pop-up
            return

        # Step 2: Find the piezo Z position where the laser spot is focused on the fiber facet.
        self.update_camera(self.camera_fiber, self.config['defaults']['laser_focusing']['low'])
        laser_focussing_power = self.config['defaults']['laser_focusing'].get('laser_power', 3)  # Get power for laser focussing from config, use 3 if it's not present
        self.set_laser_power(laser_focussing_power)
        if not self.find_focus():
            self.logger.warning('Laser not focused. Trying again...')
            if not self.find_focus():
                self.logger.warning('Laser not focused.')
                self.aligned = 'bad focus'
                self.set_laser_power(0)
                return
        self.set_laser_power(0)

        # Step 3: Identify the center of the fiber. (using the fiber LED)
        self.electronics.fiber_led = 1
        self.update_camera(self.camera_fiber, self.config['defaults']['laser_focusing']['high'])
        self.camera_fiber.trigger_camera()
        self.img_fiber_facet = self.camera_fiber.read_camera()[-1]
        img = self.img_fiber_facet
        #fiber = ut.image_convolution(img, kernel = np.ones((3,3)))
        #mask = ut.gaussian2d_array((int(fiber.shape[0]/2),int(fiber.shape[1]/2)),20000,fiber.shape)
        #fibermask = fiber * mask
        ksize = 15
        kernel = ut.circle2d_array((int(ksize/2), int(ksize/2)), 5, (ksize,ksize)) * 5.01
        kernel = (kernel - np.mean(kernel)) / np.std(kernel)
        fiber = (img - np.mean(img)) / np.std(img)
        fibermask = ut.image_convolution(fiber, kernel=kernel)
        fiber_center = np.argwhere(fibermask==np.max(fibermask))[0]
        # print(fiber_center, [389, 595])

        self.logger.info(f'TEST fiber center is {fiber_center}')
        self.electronics.fiber_led = 0
        self.update_camera(self.camera_fiber, self.config['defaults']['laser_focusing']['low'])

        # Step 4: Using the fiber camera position the laser spot onto the previously determined center of the fiber.
        self.set_laser_power(laser_focussing_power)
        time.sleep(.05)
        self.align_laser_coarse(fiber_center)

        # Step 5: Use the laser at max power and the scattered light in the microscope camera to optimize incoupling
        self.set_laser_power(99)
        self.update_camera(self.camera_microscope, self.config['defaults']['microscope_focusing']['high'])
        time.sleep(.1)
        self._trigger_camera_auto_range = True  # When
        if not self.align_laser_fine():
            self.logger.warning('Low scattering detected')
            self.aligned = 'low scattering'  # This will signal to the GUI to show a pop-up
            return

        self.set_live(self.camera_microscope, True)
        self.aligned = True

        self.logger.info('TEST Alignment done')

    def validate_initial_fiber_position(self):
        """
        Check if the (unfocused) fiber facet is the expected location.
        Determines the center of mass of the unfocused fiber facet (with the fiber LED).
        The expected location can be retrieved from config, or from arduino (if it's not in the config).
        The tolerance is retrieved from config (or a default of 50 is used).

        return: (bool) True if distance between center of (unfocused) fiber and the expected location is less than the tolerance.
        """
        time.sleep(0.01)
        self.electronics.fiber_led = 1
        # Get the expected location from config file, or from arduino memory:
        core_x, core_y = self.config['defaults'].get('core_position', self.electronics.factory_cam)
        self.logger.info(f"Expected fiber center: ({core_y}, {core_x})")
        self.update_camera(self.camera_fiber, self.config['defaults']['laser_focusing']['high'])
        time.sleep(0.01)
        self.camera_fiber.trigger_camera()
        img = self.camera_fiber.read_camera()[-1]

        cm = ndimage.measurements.center_of_mass(img**2)
        self.logger.info(f"Fiber center of mass at {cm}")
        dist = ((cm[0]-core_y)**2 + (cm[1]-core_x)**2)**0.5
        tolerance = self.config['defaults'].get('core_position_tolerance', 50)
        self.logger.info(f"Distance = {dist}. Tolerance = {tolerance}")
        # self.logger.info(f'stored coord {(core_y, core_x)} detected coord {cm}, distance: {dist}')
        self.electronics.fiber_led = 0
        return dist < tolerance

    @staticmethod
    def focus_merit(img, core_x, core_y, tolerance, half_size=125):
        """
        A helper function that calculates a figure of merit for how well the laser is in focus on the fiber facet.
        return : (int) The value to be maximized
        """
        dark = img.min()
        mx = img.max()
        bright = int((mx - dark) * 0.9 + dark)  # the 90% value between min and max value
        dark = int((bright - dark) * 0.1 + dark)  # the 10% value between "bright" and the min value: i.e. 9%
        fom = (img < dark).sum() - (img > bright).sum()
        fom2 = ((img < dark).sum() - (img > bright).sum()) * mx
        # print(dark, bright, fom, fom2)
        return fom2

    # @staticmethod
    # def focus_merit(img, core_x, core_y, tolerance, half_size=125):
    #     """
    #     A helper function that calculates a figure of merit for how well the laser is in focus on the fiber facet.
    #     return : (int) The value to be maximized
    #     """
    #     # Note that the fiber facet is roughly 250 x 250 pixels large
    #
    #     img = img[core_y-half_size+1:core_y+half_size, core_x-half_size+1:core_x+half_size]
    #
    #     # create normalized weight array that
    #     X, Y = np.meshgrid(np.arange(-half_size+1, half_size), np.arange(-half_size+1, half_size))
    #     R = (X**2+Y**2)**0.5 / tolerance * np.pi * 0.5
    #     R[R > np.pi] = np.pi
    #     weight = 1 + np.cos(R)
    #     weight /= weight.sum()
    #
    #     mx_weighted = (weight * img).max()
    #
    #     weight2 = ((X ** 2 + Y ** 2)**0.5 / tolerance - 1) * np.pi/2
    #     weight2[weight2 < 0] = np.sin(weight2[weight2 < 0])
    #     weight2 += 2
    #
    #
    #     dark = img.min()
    #     mx = img.max()
    #     bright = int((mx - dark) * 0.9 + dark)  # the 90% value between min and max value
    #     dark = int((bright - dark) * 0.1 + dark)  # the 10% value between "bright" and the min value: i.e. 9%
    #
    #     darkness_norm = (img < dark).sum() / (half_size*2-1)**2  # this value will be less than 1 but approaches 1 for a focused spot
    #     brightness_norm = (img > bright).sum() / 250
    #     fom = (img < dark).sum() - (img > bright).sum()
    #     fom2 = ((img < dark).sum() - (img > bright).sum()) * mx
    #     # print(dark, bright, fom, fom2)
    #     return fom2

    def find_focus(self):
        """
        The procedure above has some drawbacks/risks.
        The camera will usually saturate, so looking for the brightest spots won't be reliable.
        Other things to look for could be the smallest saturated spot:
        - The number of saturated pixels is very small
        - The number of black pixels is very high

        :return: None
        """
        speeds = [2, 3, 5, 8, 13, 21, 35, 58]  # will be used in reverse order
        # speeds = [3, 5, 7, 10, 15, 23, 34, 51]  # will be used in reverse order
        self.camera_fiber.trigger_camera()
        img = self.camera_fiber.read_camera()[-1]

        if SKIP_ALIGNING:
            self.img_find_focus = img
            return

        # Testing a new figure of merit for the laser being focussed
        # It will look for "as much dark pixels as possible" and "as few very bright pixels as possible"

        core_x, core_y = self.config['defaults'].get('core_position', self.electronics.factory_cam)
        tolerance = self.config['defaults'].get('core_position_tolerance', 50)

        current = MainSetup.focus_merit(img, core_x, core_y, tolerance)
        direction = len(speeds) % 2
        speed = speeds.pop()
        while self.active:
            previous = current
            self.logger.info(f'TEST moving with speed {speed} in direction {direction}')
            self.electronics.move_piezo(speed, direction, self.config['electronics']['focus_axis'])
            time.sleep(.05)
            self.camera_fiber.trigger_camera()
            img = self.camera_fiber.read_camera()[-1]
            current = MainSetup.focus_merit(img, core_x, core_y, tolerance)
            print(current)
            if current < previous:
                if not speeds:
                    break
                # Alternate direction, while ensuring the last move will always have direction=1 (i.e. "positive" piezo move)
                direction = len(speeds) % 2
                speed = speeds.pop()
        self.img_find_focus = img

        self.img_align_laser_course = img
        threshold = self.config['defaults'].get('alignment', {}).get('focus_threshold', 310e6)
        self.logger.info(f'Focus value = {current}. Threshold = {threshold}')
        return current > threshold


    def align_laser_coarse(self, fiber_center):
        """
        Aligns the focussed laser beam to the previously detected center of the fiber.
        
        :param fiber_center: coordinates of the center of the fiber 
        :type fiber_center: array or tuple of shape (2,)
        :returns: None
        
        .. todo:: consider more suitable ways to accurately detect laser beam center
        """
        if SKIP_ALIGNING:
            self.camera_fiber.trigger_camera()
            self.img_align_laser_course = self.camera_fiber.read_camera()[-1]
            return

        assert len(fiber_center) == 2

        # First reduce gain until camera doesn't saturate anymore:
        self.camera_fiber.trigger_camera()
        img = self.camera_fiber.read_camera()[-1]
        while img.max() > 254 and self.camera_fiber.gain > 0:
            new_gain = max(0, self.camera_fiber.gain - 1)
            self.logger.info(f'Reducing fiber camera gain to {new_gain}')
            self.camera_fiber.gain = new_gain
            self.camera_fiber.trigger_camera()
            img = self.camera_fiber.read_camera()[-1]

        axis = self.config['electronics']['horizontal_axis']
        for idx, c in enumerate(fiber_center):
            self.logger.info(f'TEST start aligning axis {axis} at index {idx}')
            direction = 0
            speed = 3
            self.camera_fiber.trigger_camera()
            img = self.camera_fiber.read_camera()[-1]
            mask = ut.gaussian2d_array(fiber_center, 19000, img.shape)
            mask = mask > 0.66*np.max(mask)
            img = img * mask
            img = 1*(img > 0.8*np.max(img))
            lc = ut.centroid(img)
            val_new = lc[idx]-c
            while self.active:
                val_old = val_new
                if val_new > 0:
                    direction = 0
                elif val_new < 0:
                    direction = 1
                self.logger.info(f'TEST moving with speed {speed} in direction {direction}')
                self.electronics.move_piezo(speed, direction,axis)
                time.sleep(.1)
                self.camera_fiber.trigger_camera()
                img = self.camera_fiber.read_camera()[-1]
                img = img * mask
                img = 1*(img > 0.8*np.max(img))
                lc = ut.centroid(img)
                val_new = lc[idx]-c
                self.logger.info(f'TEST last distances are {val_old}, {val_new} to centroid at {lc}')
                if np.sign(val_old) != np.sign(val_new): 
                    if speed == 1:
                        break
                    speed = 1
            axis = self.config['electronics']['vertical_axis']


    # def align_laser_fine(self):
    #     """ Maximises the fiber core scattering signal seen on the microscope cam by computing the median along axis 0.
    #     Idea: Median along axis 0 is highest for the position of fiber center even with bright dots from impurities in the image
    #     Procedure: Move until np.max(median)/np.min(median) gets smaller then change direction
    #
    #     :return: None
    #
    #     .. todo:: Test how robustly this detects the fiber core and not anything else
    #     .. todo:: (Aron) From my manual alignment attempts (in the SM fiber)), I suspect this approach will not find optimal alignment.
    #               The steps in one direction ("negative") are so large you can drop over 50%. I would suggest to sweep
    #               once through the maximum in positive direction to deduce what the maximum roughly should be, then step
    #               back far enough in negative direction and step in positive direction until a certain percentage of the
    #               deduced maximum was achieved.
    #     """
    #     axis = self.config['electronics']['horizontal_axis']
    #     for i in range(2):
    #         self.logger.info(f'TEST start optimizing axis {axis}')
    #         check = False
    #         direction = 0
    #         self.camera_microscope.trigger_camera()
    #         time.sleep(0.1)  # For debugging
    #         img = self.camera_microscope.read_camera()[-1]
    #         median = np.median(img, axis=0)
    #         val_new = np.max(median)/np.min(median)
    #         axis = self.config['electronics']['vertical_axis']
    #         while self.active and not SKIP_ALIGNING:
    #             val_old = val_new
    #             self.electronics.move_piezo(1, direction, axis)
    #             time.sleep(.1)
    #             self.camera_microscope.trigger_camera()
    #             img = self.camera_microscope.read_camera()[-1]
    #             median = np.median(img, axis=0)
    #             val_new = np.max(median)/np.min(median)
    #             if val_old > val_new:
    #                 direction = (direction + 1) % 2
    #                 if check:
    #                     self.electronics.move_piezo(1, direction, axis)
    #                     break
    #                 check = True
    #         axis = self.config['electronics']['vertical_axis']
    #
    #     self.img_align_laser_fine = img

    def align_laser_fine(self):
        """
        Optimize laser spot position by looking at scattered light in fiber with the microscope camera.

        Iterate both axes multiple times, always finishing piezo motion in positive direction.
        Because the piezo steps are very large, don't use a drop in intensity as the moment to stop, but the expected
        maximum encountered on a previous sweep.

        """
        def figure_of_merit():
            """Local helper function that calculates a figure of merit to be optimized"""
            self.camera_microscope.trigger_camera()
            time.sleep(0.1)  # For debugging
            img_ = self.camera_microscope.read_camera()[-1]
            # bin 4 pixels along the fiber into 1:
            img4 = img_[:img_.shape[0]-(img_.shape[0]%4), :].astype(float)
            img4 = img4[::4] + img4[1::4] + img4[2::4] + img4[3::4]
            pos = np.argmax(img4, axis=1)
            ampl = np.max(img4, axis=1)
            try:
                center_fit = np.polyfit(np.arange(img4.shape[0]), pos, w=ampl, deg=1)
                center = lambda p: np.polyval(center_fit, p)
                if not 0 < center_fit[1] < img4.shape[1] or not np.polyval(center_fit, img4.shape[0]):
                    raise  # don't use fit if the line is fully in the image
            except:
                # Fall back on combination of position of maximum and the center of the window
                self.logger.info('Discarding fit')
                center_fit = [0, img4.shape[1]//2]
                center = lambda p: (np.polyval(center_fit, p) + 2 * pos[p]) / 3
            w = lambda p, sig: np.exp(-((np.arange(img4.shape[1]) - center(p)) / sig) ** 2 / 2)
            sig_peak = 6
            sig_surround = 9
            weight_peak = lambda p: w(p, sig_peak)/w(p, sig_peak).sum()
            weight_surrounding = lambda p: (1-2*w(p, sig_surround))*(w(p, sig_surround)<0.5) / \
                                          ((1-2*w(p, sig_surround))*(w(p, sig_surround)<0.5)).sum()
            peaks = np.zeros(img4.shape[0])
            surroundings = np.zeros(img4.shape[0])
            for p in range(img4.shape[0]):
                peaks[p] = np.sum(img4[p, :] * weight_peak(p))
                surroundings[p] = np.sum(img4[p, :] * weight_surrounding(p))
            peaks_corr = peaks - surroundings
            # Only use the 90% of the data that has the lowest value for surrounding.
            # (In the hope to be less sensitive to small floating scatterers)
            peaks_corr = peaks_corr[np.argsort(surroundings)[:int(len(surroundings) * 0.9)]]
            return img_, np.median(peaks_corr)

            #
            # peaks, next_to_peaks = 2*[np.zeros(img.shape[0])]
            # from scipy import optimize
            # peak_func = lambda ampl, offset, width: ampl*np.exp(-0.5*((np.arange(img.shape[1])-offset)/width)**2)
            # for p in range(img.shape[0]):
            #     optimize.fmin(peak_func, x0=[np.polyval(center_fit)])
            #     center = int(np.round(np.polyval(center_fit, p)))
            #     on_peak_line = img[p, max(center-2, 0): min(center+2, img.shape[1])]
            #     peaks[p] = np.mean(on_peak_line)
            #     next_to_peaks[p] =
            #
            #
            #
            # N = 8
            # cross_cuts = np.empty((N, img.shape[1]))
            # means, stdevs, position_of_max = [np.zeros(N)]*3
            # for section in range(8):
            #     img_section = img[int(img.shape[0]/N*section):int(img.shape[0]/N*(section+1)), :]
            #     cross_cuts[section, :] = np.median(img_section, axis=0)
            #     position_of_max[section] = np.argmax(cross_cuts[section, :])
            #     means[section] = np.mean(img_section[:])
            #     stdevs[section] = np.std(img_section[:])
            #
            # maxima = np.maximum(cross_cuts, axis=1)
            # peak_width = np.sum((cross_cuts / maxima) > 0.8, axis=1)
            # most_deviating_section_by_peak_width = np.argmax(np.abs(peak_width - np.mean(peak_width)))
            # most_deviating_section_by_mean = np.argmax(np.abs(means - np.mean(means)))
            # most_deviating_section_by_stdev = np.argmax(np.abs(stdevs - np.mean(stdevs)))
            # most_deviating_section_by_position_of_max = np.argmax(np.abs(position_of_max - np.mean(position_of_max)))
            # deviating_indices = [most_deviating_section_by_peak_width, most_deviating_section_by_mean, most_deviating_section_by_stdev, most_deviating_section_by_position_of_max]
            # print('deviating_indices', deviating_indices)
            # indices = np.setdiff1d(np.arange(N), deviating_indices)
            # cross_cuts = cross_cuts[indices, :]
            # # typical_bg = np.median(np.mean(cross_cut, axis=0))
            # np.maximum(cross_cuts, axis=1)
            # np.argmax(means)
            # np.argmax(stdevs)
            #
            # median = np.median(img, axis=0)
            # figure = np.max(median)/np.min(median)
            # # self.logger.info(f'Figure of merit: {figure}')
            # return img, figure

        # def figure_of_merit():
        #     self.camera_microscope.trigger_camera()
        #     time.sleep(0.1)  # For debugging
        #     img = self.camera_microscope.read_camera()[-1]
        #     median = np.median(img, axis=0)
        #     figure = np.max(median)/np.min(median)
        #     # self.logger.info(f'Figure of merit: {figure}')
        #     return img, figure

        if SKIP_ALIGNING:
            self.img_align_laser_fine, _ = figure_of_merit()
            return True

        img, current = figure_of_merit()
        highest_values = []
        N = 2  # increase the number of sweeps if desired
        # step = 0
        steps_taken = [[0, 0], [0, 0]]  # steps_taken[axis][direction]
        for iteration in range(N):
            for axis in [0, 1]:
                for i, direction in enumerate([0, 1, 0, 1]):
                    highest_values.append(current)
                    passed_optimum = False
                    # if direction == 0:
                    #     max_steps = int(min(0, steps_taken[axis][1] / 1.5 - steps_taken[axis][0]))
                    # else:
                    #     max_steps = int(min(0, steps_taken[axis][0] * 1.5 - steps_taken[axis][1]))
                    # print('max steps:', iteration, axis, direction, max_steps)
                    for step in range(4 + 2*direction):#, + max_steps):  # put some kind of limit on the number of steps, just in case
                        previous = current
                        self.electronics.move_piezo(1, direction, axis)
                        steps_taken[axis][direction] += 1
                        img, current = figure_of_merit()
                        print(current)
                        highest_values[-1] = max(highest_values[-1], current)
                        # On the last sweep in "positive" direction, exit loop also if figure of merit reaches 99% of the maximum
                        # found so far. (This is to reduce the chance of stepping over the optimum)
                        if i == 3 and current > 0.99 * max(highest_values):
                            break
                        if current < previous:
                            if i == 1 and not passed_optimum:  # in the first forward sweep, take an extra step after detecting a decline
                                passed_optimum = True
                                continue
                            break
        img, final = figure_of_merit()
        highest_values.append(final)
        # print(highest_values)
        self.logger.info('steps taken: ax0 -{} +{}, ax1 -{} +{}'.format(*steps_taken[0], *steps_taken[1]))
        self.img_align_laser_fine = img
        scattering_threshold = self.config['defaults'].get('alignment', {}).get('scattering_threshold', 50)
        self.logger.info(f'Scattering value = {final}. Threshold = {scattering_threshold}')
        # This quality threshold needs to be investigated
        return (final > scattering_threshold)


    # @make_async_thread
    def find_ROI(self, crop=False):
        """Assuming alignment, this function fits a gaussian to the microscope images cross-section to compute a ROI
        """
        self.update_camera(self.camera_microscope, self.config['defaults']['microscope_focusing']['high'])
        self.set_laser_power(99)
        img = self.camera_microscope.temp_image
        self.img_microscope_before_final_alignment = img
        self.set_live(self.camera_microscope, False)
        while self.camera_microscope.continuous_reads_running:
            time.sleep(.1)
        self.logger.info(f'TEST imgshape {img.shape}')

        if SKIP_ALIGNING:
            width = self.config['defaults']['core_width']
            roi = self.camera_microscope.ROI
            new_roi = (roi[0], (roi[1][0] + (roi[1][1] - width) // 2, width))
            time.sleep(.15)
            self.camera_microscope.ROI = new_roi
            self.set_live(self.camera_microscope, True)
            return
        # measure = np.sum(img, axis=0)

        # # This seems to work:
        # measure = np.median(img, axis=0)
        #
        # # This also seems to work:
        # # Cut the image up in to N sections, take the average for each, followed by the median of these
        # sections = 11  # recommend an odd number
        # means_of_sections = np.zeros((sections, img.shape[1]))
        # for i in range(sections):
        #     means_of_sections[i, :] = img[int(round(img.shape[0]/sections*i)):int(round(img.shape[0]/sections*(i+1))), :].mean(axis=0)
        # measure += np.median(means_of_sections, axis=0)

        # Another approach, would be to fit on multiple sections,
        # and then take the median of the fitting results:
        # (the objective is to discard sections that don't contribute to a proper fit due to dust/air)
        fitted_centers = []
        sections = 9  # recommend an odd number
        for i in range(sections):
            section = img[int(round(img.shape[0]/sections*i)):int(round(img.shape[0]/sections*(i+1))), :]
            measure = section.mean(axis=0)
            argmax_measure = np.argwhere(measure == np.max(measure))[0][0]
            argmax_measure = (argmax_measure + measure.shape[0]/2)/2  # bias it towards the center
            cx = int(measure.shape[0] / 2)
            measure = measure / np.max(measure)
            xvals = np.linspace(0, 1, measure.shape[0]) - 0.5
            gaussian1d = lambda x, mean, var, A, bg: A * np.exp(-(x-mean)**2 / (2 *var)) + bg
            popt, pcov = optimize.curve_fit(
                gaussian1d,
                xvals,
                measure,
                p0=[argmax_measure/measure.shape[0] - 0.5, 0.1, np.max(measure)-np.min(measure), np.min(measure)],
                bounds=([-0.5, 0, 0, 0],[0.5, 1, 1,1]))
            # self.logger.info(f'TEST {popt}')
            cx += int(popt[0] * measure.shape[0])
            # width = 2 * int(2 * np.sqrt(popt[1] * measure.shape[0]))
            fitted_centers.append(cx)

        # self.logger.info(f'ROI optimized with width {width}')
        self.logger.info(f'Fitted centers: {fitted_centers}')
        cx = np.median(fitted_centers)
        self.logger.info(f'Median fit of center: {cx}')
        width = self.config['defaults']['core_width']
        current_roi = self.camera_microscope.ROI
        new_y_offset = current_roi[1][0]+cx-width
        new_roi = (current_roi[0], (new_y_offset, 2*width+1))
        self.logger.info(f'setting ROI width: {width}')
        self.camera_microscope.ROI = new_roi


        self.set_live(self.camera_microscope, True)

    # def reset_waterfall(self):
    #     self.waterfall_image = np.empty((self.camera_microscope._driver.Width.Value, self.config['GUI']['length_waterfall']))
    #     self.waterfall_image.fill(np.nan)

    @make_async_thread
    def save_waterfall(self):
        """Assuming a set ROI, this function calculates a waterfall slice per image frame and sends it to a MovieSaver instance
        """
        self.start_saving_images()
        img = self.camera_microscope.temp_image

        smooth = lambda line: line[1:-1:2] * 2 + line[:-2:2] + line[2::2]

        if USE_TEST_DATA:
            import h5py
            f = h5py.File(USE_TEST_DATA, 'r')
            meta = yaml.safe_load(f['data']['metadata'][()].decode())
            frames = meta['frames']
            increment = int(max(1, self.config['GUI']['refresh_time'] / Q_(meta['exposure']).m_as('ms')))
            print(self.config['GUI']['refresh_time'], Q_(meta['exposure']).m_as('ms'), increment)
            i = SKIP_FIRST_LINES_IN_TEST_DATA
            img = f['data']['timelapse'][:, :3]

        # img.shape[0]
        self.waterfall_image = np.zeros((len(smooth(img[:, 1])), self.config['GUI']['length_waterfall']))#, dtype=np.int32)
        # median = np.zeros((img.shape[0],))
        N_median = 10
        buffer = np.tile(img[:, [0]], (1, N_median))
        avg_median = np.median(buffer, axis=1)
        div = np.ones_like(avg_median)
        # avg_std = avg_median/10
        buffer_index = 0
        # self.reset_waterfall()
        refresh_time_s = self.config['GUI']['refresh_time'] / 1000


        while self.active:
            img = self.camera_microscope.temp_image
            new_slice = np.sum(img, axis=1)
            if USE_TEST_DATA:
                new_slice = f['data']['timelapse'][:, i]
                i = (i + increment) % frames

            curr_median = np.median(buffer, axis=1)
            # avg_std = (avg_std * 3 + np.std(buffer, axis=1))/4
            avg_median = (avg_median * 1 + curr_median) / 2
            buffer[:, buffer_index] = new_slice
            buffer_index = (buffer_index + 1) % N_median

            self.waterfall_image = np.roll(self.waterfall_image, shift=-1, axis=1)
            div = (div + np.maximum(0.5, np.sqrt(avg_median / np.median(avg_median))))/2
            new_line = (new_slice - avg_median) / div  # np.sqrt(np.maximum(avg_median, 1.0))  # np.sqrt(1+avg_std)
            self.waterfall_image[:, -1] = smooth(new_line)#[::2] + new_line[1::2]  # bin in horizontal direction in 2 pixels

            _min, _max = self.waterfall_image[:, -20:].min(), self.waterfall_image[:, -20:].max()
            dif = (_max - _min)/10
            self.waterfall_image_limits[0] = self.waterfall_image_limits[0] * 0.97 + 0.03 * _min
            self.waterfall_image_limits[1] = self.waterfall_image_limits[1] * 0.97 + 0.03 * (_max+2*dif)
            time.sleep(refresh_time_s - time.time() % refresh_time_s)

        self.stop_saving_images()
        if USE_TEST_DATA:
            f.close()
        
    def start_saving_images(self):
        if self.saving:
            self.logger.warning('Saving process still running: self.saving is true')
        if self.saving_process is not None and self.saving_process.is_alive():
            self.logger.warning('Saving process is alive, stop the saving process first')
            return

        self.saving = True
        base_filename = self.config['info']['files']['filename']
        file = self.get_filename(base_filename)
        self.saving_event.clear()
        if self.saving_images:
            alignment_images = {'focus_microscope': self.img_focus_microscope,
                                'focus_laser': self.img_find_focus,
                                'fiber_facet': self.img_fiber_facet,
                                'align_laser': self.img_align_laser_course,
                                'scattering_optimization': self.img_align_laser_fine}
        else:
            alignment_images = {}
        self.saving_process = WaterfallSaver(
            file,
            self.config['info']['files']['max_memory'],
            self.camera_microscope.frame_rate,
            self.saving_event,
            self.camera_microscope.new_image.url,
            topic='new_image',
            alignment_images=alignment_images,
            metadata=self.camera_microscope.config.all(),
            versions = {'software_version': self.VERSION, 'firmware_version': self.electronics.driver.query('IDN')}
        )

    def stop_saving_images(self):
        self.camera_microscope.new_image.emit('stop')
        # self.emit('new_image', 'stop')

        # self.saving_event.set()
        time.sleep(.05)

        if self.saving_process is not None and self.saving_process.is_alive():
            self.logger.warning('Saving process still alive')
            time.sleep(.1)
        self.saving = False

    @Action
    def snap_image(self, camera):
        self.logger.info(f'Trying to snap image on {camera}')
        if camera.continuous_reads_running: 
            self.logger.warning('Continuous reads still running')
            return
        camera.acquisition_mode = camera.MODE_SINGLE_SHOT
        camera.trigger_camera()
        camera.read_camera()
        self.display_camera = camera
        self.logger.info('Snap Image complete')

    @Action    
    def set_live(self, camera, live):
        logstring = {True: "Turn on", False: "Turn off"}
        self.logger.info(f'{logstring[live]} live feed on {camera}')
        if camera.continuous_reads_running:
            if live: return
            camera.stop_continuous_reads()
            camera.stop_free_run()
            self.logger.info('Continuous reads ended')
            self.display_camera = None
        else:
            if not live: return
            camera.start_free_run()
            camera.continuous_reads()
            self.display_camera = camera
            self.logger.info('Continuous reads started')

    def update_camera(self, camera, new_config):
        """ Updates the properties of the camera.
        new_config should be dict with keys exposure_time and gain
        """
        self.logger.info('Updating parameters of the camera')
        camera.config.update({
                'exposure': Q_(new_config['exposure']),
                'gain': float(new_config['gain']),
        })
        camera.config.apply_all()

    def set_laser_power(self, power: int):
        """ Sets the laser power, taking into account closing the shutter if the power is 0
        """
        self.logger.info(f'Setting laser power to {power}')
        power = int(power)

        self.electronics.scattering_laser = power
        self.config['electronics']['laser']['power'] = power

    def get_latest_image(self):
        return self.camera_microscope.temp_image

    def get_waterfall_image(self):
        return self.waterfall_image

    def load_configuration(self, *args, **kwargs):
        super().load_configuration(*args, **kwargs)
        # To allow the use of environmental variables like %HOMEPATH%
        folder = self.config['info']['files']['folder']
        for key, val in os.environ.items():
            folder = folder.replace('%'+key+'%', val)
        self.config['info']['files']['folder'] = os.path.abspath(folder)

    def prepare_folder(self) -> str:
        """Creates the folder with the proper date, using the base directory given in the config file"""
        base_folder = self.config['info']['files']['folder']
        today_folder = f'{datetime.today():%Y-%m-%d}'
        folder = os.path.join(base_folder, today_folder)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        return folder

    def get_filename(self, base_filename: str) -> str:
        """Checks if the given filename exists in the given folder and increments a counter until the first non-used
        filename is available.

        :param base_filename: must have two placeholders {description} and {i}
        :returns: full path to the file where to save the data
        """
        if base_filename == "":
            base_filename = self.config['info']['files']['filename']
        folder = self.prepare_folder()
        i = 1
        description = self.config['info']['files']['description']
        while os.path.isfile(os.path.join(folder, base_filename.format(description=description, i=i))):
            i += 1
        return os.path.join(folder, base_filename.format(description=description, i=i))

    def finalize(self):
        if self.finalized:
           return
        self.logger.info('Finalizing calibration experiment')
        self.active = False
        
        if self.saving:
            self.logger.debug('Finalizing the saving images')
            self.stop_saving_images()
        self.saving_event.set()

        with open('config_user.yml', 'w') as f:
            yaml.dump(self.config, f, default_flow_style=False)

        if self.camera_microscope:
            self.camera_microscope.finalize()
        if self.electronics and self.electronics.initialized:
            self.set_laser_power(0)
            self.electronics.finalize()

        super(MainSetup, self).finalize()
        self.finalized = True

if __name__ == '__main__':
    from NanoCETPy import BASE_PATH
    self = MainSetup()
    if not (config_filepath := BASE_PATH / 'config_user.yml').is_file():
        config_filepath = BASE_PATH / 'resources/config_default.yml'
    self.load_configuration(config_filepath, yaml.UnsafeLoader)
    self.initialize()
    self.validate_initial_fiber_position()