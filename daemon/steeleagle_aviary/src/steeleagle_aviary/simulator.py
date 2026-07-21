import argparse
import importlib
from typing import Optional
from dataclasses import dataclass
import logging
import argparse
import inspect
from colorhash import ColorHash
# Panda3D imports
from panda3d.core import CardMaker, AmbientLight, DirectionalLight, Fog
from panda3d.core import FrameBufferProperties, WindowProperties, PerspectiveLens, TransformState
from panda3d.core import Texture, TextureStage
from panda3d.core import ConfigVariableString
from panda3d.core import NodePath, GraphicsOutput, GraphicsPipe, LVector3f, TextNode, PandaNode
from direct.showbase.ShowBase import ShowBase
from direct.gui.DirectGui import DirectLabel
from panda3d.core import loadPrcFileData
from panda3d.core import GraphicsPipeSelection
from panda3d.core import BitMask32
# Numerical imports
import numpy as np
# Simulator imports
from steeleagle_aviary.interfaces.base import Interface
from steeleagle_aviary.engines.base import Engine
from steeleagle_aviary.datatypes import GroundHolder, CameraHolder, GeodeticPoint
from steeleagle_aviary.vehicle import Vehicle
from steeleagle_aviary.actor import Actor

"""Simulator for kinematic vehicle motion.

Simulates kinematic vehicle motion, either flying or on
the ground. Also has features for logical inference with
mock AI engines and actors which can be configured to
move around the space.
"""

logger = logging.getLogger('Aviary/simulator')

# Ground size constant
GROUND_SIZE = 100

# Set headless mode
loadPrcFileData("", """
  window-type none
""")

def import_class(module: str, cls: any) -> any:
    """Imports a class from a module.

    Imports a class type from a given module name. If there are more than
    one class of that type, or if the class is not importable, then an
    ImportError will be raised. The module must be importable in the
    sys.path before this function is called.

    Args:
        module (str): module path to import from
        cls (any): class type to import

    Returns:
        any: imported class

    Raises:
        ImportError: if name cannot be found or the import fails
    """
    mod = importlib.import_module(module)
    matches = []
    for name, obj in inspect.getmembers(mod, inspect.isclass):
        if (issubclass(obj, cls)
            and obj is not cls
            and obj.__module__ == mod.__name__):
            matches.append(obj)
    if len(matches) != 1:
        raise ImportError(f'Number of matching classes is not 1, instead it is {len(matches)}')
    return matches[0]


class Simulator(ShowBase):
    def __init__(self):
        super().__init__()

        # Lighting
        ambient = AmbientLight('ambient')
        ambient.setColor((0.7, 0.7, 0.7, 1))
        self.render.setLight(self.render.attachNewNode(ambient))

        directional = DirectionalLight('directional')
        directional.setDirection(LVector3f(-1, -1, -2))
        directional.setColor((0.8, 0.8, 0.8, 1))
        self.render.setLight(self.render.attachNewNode(directional))

        # Holds references to all moving entities
        self.actors = {}
        self.vehicles = {}
        self.interfaces = {}

        # Trace support values
        self.active_trace = False
        self.trace_name = None

        # Simulation anchor point that ties the simulation to geodetic space.
        # This point is chosen to be the first actor/vehicle's geodetic
        # origin point.
        self.anchor = None

        # Create a pipe manually to perform default initialization
        # and set self.pipe (not automatically set due to no window mode)
        gps = GraphicsPipeSelection.get_global_ptr()
        self.pipe = gps.make_default_pipe()
        if not self.pipe:
            raise RuntimeError('Failed to create a GraphicsPipe (no GLX/EGL/etc available?)')

        # Build a dummy 1x1 offscreen buffer to act as the parent window.
        # This is necessary so that Panda3D can render offscreen buffers
        # without building a blank window.
        fbp = FrameBufferProperties()
        fbp.setRgbColor(True)
        fbp.setAlphaBits(0)
        fbp.setDepthBits(1)

        wp = WindowProperties()
        wp.setSize(1, 1)

        self.win = self.graphicsEngine.make_output(
            self.pipe,
            'MainHost',
            0,
            fbp,
            wp,
            GraphicsPipe.BFRefuseWindow,
            None,
            None
        )

        # Simulation tick task
        self.taskMgr.add(self.simulate, 'simulate-task')

    def add_vehicle(self, name: str, interface: str, interface_args: dict[str, any], origin: GeodeticPoint, **kwargs) -> Vehicle:
        """Add a moveable vehicle to the simulation.

        Adds a vehicle and an actuation interface (SteelEagle) to the
        simulation. Motion is kinematic so there is no collision detection.

        Args:
            name (str): name of the vehicle (must be unique)
            interface (str): interface module name for this vehicle (must be importable)
            interface_args (dict[str, any]): interface arguments
            origin (GeodeticPoint): origin geodetic point of the vehicle
            kwargs (dict): kwargs for the vehicle initialization

        Returns:
            Vehicle: newly created vehicle object
        """
        # Check if name already exists, in this case ignore!
        if name in self.vehicles:
            logger.warning(f'Vehicle {name} already exists, ignoring')
            return None

        # Check if camera parameters are included, otherwise set defaults
        fov = (128, 72)
        size = (1280, 720)
        if 'camera' in kwargs:
            fov = kwargs['camera']['fov']
            size = kwargs['camera']['size']

        # Set up texture and graphics buffer
        buffer = self.graphicsEngine.make_output(
            self.pipe,
            f'Image Buffer [{name}]',
            -2,
            FrameBufferProperties(),
            WindowProperties.size(*size),
            GraphicsPipe.BFRefuseWindow,
            self.win.getGsg(),
            self.win
        )
        texture = Texture()
        buffer.addRenderTexture(texture, GraphicsOutput.RTMCopyRam)

        # Ground plane horizontal
        cam_mask = BitMask32.bit(len(self.vehicles)) # create mask ID from length of vehicle list
        cm = CardMaker(f'Ground [{name}]')
        cm.setFrame(-GROUND_SIZE, GROUND_SIZE, -GROUND_SIZE, GROUND_SIZE)
        ground = self.render.attachNewNode(cm.generate())
        ground.setHpr(0, -90, 0)  # horizontal X-Y plane
        tex = self.loader.load_texture('maps/envir-ground.jpg')
        tex.set_wrap_u(Texture.WM_repeat)
        tex.set_wrap_v(Texture.WM_repeat)
        ground.set_texture(tex)
        ts = TextureStage.get_default()
        ground.set_tex_scale(ts, 20, 20)  # Repeat 4x4 times
        ground.setBin('fixed', 0)
        ground.hide(BitMask32.allOn()) # Hide on all cameras except this one
        ground.show(BitMask32.bit(0) | cam_mask)
        ground_holder = GroundHolder(ground, ts)

        # Set up lens according to camera intrinsics
        lens = PerspectiveLens()
        lens.set_film_size(*size)
        lens.set_fov(*fov)
        lens.setNear(0.1)
        lens.setFar(1000.0)
        camera = self.makeCamera(buffer, lens=lens, camName=f'Image Camera [{name}]')
        camera.node().setCameraMask(BitMask32.bit(0) | cam_mask)
        proj = camera.attachNewNode('proj-holder')

        camera.reparentTo(self.render)
        model = self.loader.loadModel('models/misc/objectHandles')
        model.setScale(0.5, 0.5, 0.5)
        model.setBin('fixed', 10)
        model.reparentTo(camera)
        font = self.loader.loadFont('/usr/share/fonts/truetype/noto/NotoMono-Regular.ttf')
        text = TextNode(f'{name}_text')
        text.setFont(font)
        text.setText(name)
        text.setTextColor(0, 0, 0, 1)
        text.setCardColor(0.8, 0.8, 0.8, 0.3)
        text.setCardAsMargin(0, 0, 0, 0)
        text.setCardDecal(True)
        text_nodepath = NodePath(text)
        text_nodepath.reparentTo(camera)
        text_nodepath.setBillboardAxis(0.0)
        text_nodepath.setPos(0, 0, 1)
        text_nodepath.setScale(0.5)
        text_nodepath.setBin('fixed', 10)
        camera_holder = CameraHolder(camera, buffer, texture, proj, fov, size)

        # Set the simulation anchor if it hasn't already been set
        if not self.anchor:
            self.anchor = origin

        vehicle = Vehicle(name, origin, self.anchor, camera_holder, ground_holder)
        try:
            iface = import_class(interface, Interface)(vehicle, **interface_args)
            iface.start()
            self.interfaces[name] = iface
        except Exception as e:
            logger.error(f'Cannot instantiate interface {interface}, reason: {e}')
            return None

        logger.info(f'Added vehicle {name} at point {origin}!')
        self.vehicles[name] = vehicle
        return vehicle

    def add_actor(self, name: str, tag: str, origin: GeodeticPoint, waypoints: list, **kwargs) -> Actor:
        """Add a moveable actor to the simulation.

        Adds an actor to the simulation. Motion is kinematic so there
        is no collision detection. Actors are generally modelled as
        white cubes.

        Args:
            name (str): name of the actor
            tag (str): type of object to be reported by object detector logical inference
            origin (GeodeticPoint): origin geodetic point of the actor
            waypoints (list): list of waypoints for the actor
            kwargs (dict): kwargs for the actor initialization

        Returns:
            Actor: newly created actor object
        """

        parent = NodePath(PandaNode(name))
        parent.reparentTo(self.render)
        cube = self.loader.loadModel('models/box')

        obj_len = kwargs.get('length', 1)
        obj_width = kwargs.get('width', 1)
        obj_height = kwargs.get('height', 1)

        cube.setScale(obj_len, obj_width, obj_height) # Defaults to 1x1x1m cube if not specified in config file
        cube.reparentTo(parent)
        cube.setTextureOff(1)  # Disable default texture
        color = ColorHash(tag)
        r,g,b = color.rgb
        cube.setColor(r/255, g/255, b/255, 1)  # Normalize to 0.0-1.0 for Panda3D
        cube.setBin('fixed', 10)
        # Add a textnode with the name of the actor
        font = self.loader.loadFont('/usr/share/fonts/truetype/noto/NotoMono-Regular.ttf')
        text = TextNode(f'{name}_text')
        text.setFont(font)
        text.setText(name)
        text.setTextColor(0, 0, 0, 1)
        text.setCardColor(0.8, 0.8, 0.8, 0.3)
        text.setCardAsMargin(0, 0, 0, 0)
        text.setCardDecal(True)
        text_nodepath = NodePath(text)
        text_nodepath.reparentTo(parent)
        text_nodepath.setBillboardAxis(0.0)
        text_nodepath.setPos(0,0,1)
        text_nodepath.setScale(0.5)
        text_nodepath.setBin('fixed', 10)

        # Set the simulation anchor
        if not self.anchor:
            self.anchor = origin

        logger.info(f'Added actor {name} with tag {tag} and color {color.rgb} at point {origin} and waypoints {waypoints}!')
        actor = Actor(name, tag, origin, self.anchor, parent, waypoints, **kwargs)
        self.actors[name] = actor
        return actor

    def simulate(self, task):
        """Simulation tick, auto-called by Panda3d.
        """
        dt = globalClock.getDt()
        # Move objects
        for _, act in self.actors.items():
            act.move(dt)

        # Move vehicles
        for _, veh in self.vehicles.items():
            veh.move(dt)

        # Build a new frame
        self.graphics_engine.render_frame()

        return task.cont
