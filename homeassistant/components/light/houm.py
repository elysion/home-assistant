"""
homeassistant.components.light.houm
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Support for Houm.io lights.

"""
import time
import logging
from threading import Thread, Event

from homeassistant.components.light import ATTR_BRIGHTNESS
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers.entity import ToggleEntity

import requests
from requests.exceptions import RequestException

from socketIO_client import SocketIO, LoggingNamespace

_LOGGER = logging.getLogger(__name__)

REQUIREMENTS = ['socketIO-client==0.6.5']


class StoppableThread(Thread):
    def __init__(self):
        Thread.__init__(self)
        self.stop_event = Event()

    def stop(self):
        if self.isAlive():
            self.stop_event.set()
            self.join()


class IntervalTimer(StoppableThread):
    def __init__(self, interval, worker_func):
        super().__init__()
        self._interval = interval
        self._worker_func = worker_func

    def run(self):
        while not self.stop_event.is_set():
            self._worker_func()
            time.sleep(self._interval)


# pylint: disable=unused-argument
def setup_platform(hass, config, add_devices_callback, discovery_info=None):
    controller = HoumController(config, add_devices_callback)
    hass.bus.listen_once(EVENT_HOMEASSISTANT_STOP, controller.close_socket)


class HoumController(object):
    def __init__(self, config, add_devices_callback):
        self.socket_thread = None
        self.socket = None
        self.last_command_sent = 0
        site_key = config.get('site_key')
        if not site_key:
            _LOGGER.error(
                    "The required parameter 'site_key'"
                    " was not found in config"
            )
            return

        self.SITE_KEY = site_key
        self.config_device_data = config.get('device_data', {})
        self.protocols = config.get('protocols', {})
        self.add_devices_callback = add_devices_callback

        self.device_id_map = {}
        self.discover_lights_and_sync_statuses()

        self.reconnect_on_disconnect = True
        self.open_socket()

        self.discover_and_sync_timer = IntervalTimer(5, self.discover_lights_and_sync_statuses)
        self.discover_and_sync_timer.start()

    def open_socket(self):
        print("opening socket")
        self.socket = SocketIO(host='https://houmi.herokuapp.com', logging=LoggingNamespace)
        self.socket.on('connect', self.on_connect)
        self.socket_thread = Thread(target=self.socket.wait)
        self.socket_thread.start()
        self.socket.on('disconnect', self.reconnect)
        self.socket.on('close', self.reconnect)
        self.socket.on('error', self.reconnect)

    def reconnect(self):
        print(self.socket_thread.is_alive())
        if self.reconnect_on_disconnect:
            self.close_socket()
            self.open_socket()

    # pylint: disable=unused-argument
    def close_socket(self, args=None):
        self.reconnect_on_disconnect = False
        self.discover_and_sync_timer.stop()
        self.socket.disconnect()
        self.socket_thread.join()

    def on_connect(self):
        self.socket.emit('clientReady', {'siteKey': self.SITE_KEY})
        self.socket.on('setLightState', self.update_light)

    def update_light(self, updated_data):
        try:
            updated_device = self.device_id_map.get(updated_data['_id'])
            updated_device.bri = updated_data['bri']
            updated_device.on = updated_data['on']
            updated_device.update_ha_state()
        except AttributeError as e:
            _LOGGER.error("Invalid data received from socket: ")
            if e.args:
                _LOGGER.error(e.args)

    def discover_lights_and_sync_statuses(self):
        if (self.last_command_sent + 1) > time.time():
            return

        try:
            found_lights = self.get_lights(['binary', 'dimmable'], self.protocols)
        except RequestException:
            # There was a network related error connecting to the vera controller.
            _LOGGER.exception("Error communicating with Houm")
            return False

        new_lights = []

        for light in found_lights:
            excluded = light.deviceId in self.config_device_data and self.config_device_data[light.deviceId].get(
                    'exclude', False)
            if light.deviceId not in self.device_id_map and not excluded:
                self.device_id_map[light.deviceId] = light
                new_lights.append(light)
            else:
                light_to_update = self.device_id_map[light.deviceId]
                light_to_update.on = light.on
                light_to_update.bri = light.bri

        if new_lights:
            self.add_devices_callback(new_lights)

    def get_lights(self, type_filter=None, protocol_filter=None):
        self.last_command_sent = time.time()

        devices = []

        site_info_url = "https://houmi.herokuapp.com/api/site/" + self.SITE_KEY
        site_info = requests.get(site_info_url).json()

        lights = site_info.get('lights')

        for light in lights:
            if 'type' in light and light.get('type') == 'dimmable':
                devices.append(HoumDimmer(light, self))
            elif 'type' in light and light.get('type') == 'binary':
                devices.append(HoumSwitch(light, self))

        if not type_filter and not protocol_filter:
            return devices
        else:
            filtered_devices = []
            for light in devices:
                if (not type_filter or light.type in type_filter) \
                        and (not protocol_filter or light.protocol in protocol_filter):
                    filtered_devices.append(light)

            return filtered_devices

    def set_value(self, name, device_id, value):
        on = value if name == 'on' else value > 0
        bri = value if name == 'bri' else 255 if value else 0

        self.socket.emit('apply/light', {"_id": device_id, "on": on, "bri": bri})


class HoumDevice(ToggleEntity):
    def __init__(self, json_state, houm_controller):
        self.houmController = houm_controller

        self.on = json_state.get('on')
        self.bri = json_state.get('bri')
        self.deviceId = json_state.get('_id')
        self.type = json_state.get('type')
        self._name = json_state.get('name')
        self.protocol = json_state.get('protocol')

    def set_value(self, name, value):
        if name == 'bri':
            self.bri = value
            self.on = value > 0
        elif name == 'on':
            self.on = value

        self.houmController.set_value(name, self.deviceId, value)

    def get_value(self, name):
        if name == 'on':
            return self.on
        if name == 'bri':
            return self.bri

    @property
    def name(self):
        return self._name


class HoumSwitch(HoumDevice):
    def __init__(self, json_state, houm_controller):
        super().__init__(json_state, houm_controller)

    def turn_on(self):
        self.set_value('on', True)

    def turn_off(self):
        self.set_value('on', False)

    @property
    def is_on(self):
        return self.get_value('on')


class HoumDimmer(HoumSwitch):
    def __init__(self, json_state, houm_controller):
        super().__init__(json_state, houm_controller)

    @property
    def brightness(self):
        """ Brightness of this light between 0..255. """
        return self.get_value('bri')

    @property
    def state_attributes(self):
        attr = super().state_attributes or {}

        attr[ATTR_BRIGHTNESS] = self.brightness

        return attr

    def turn_on(self, **kwargs):
        new_brightness = 255
        if ATTR_BRIGHTNESS in kwargs:
            new_brightness = kwargs[ATTR_BRIGHTNESS]

        self.set_value('bri', new_brightness)
