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
# Python imports
import argparse
import importlib
from typing import Optional
from dataclasses import dataclass
import logging
import argparse
# Numerical imports
import numpy as np
# Utility imports
from datatypes import GroundHolder, CameraHolder, GeodeticPoint
from engines.base import get_engine_from_name, EngineHolder
# Entity imports
from vehicle import Vehicle
from actor import Actor
# Toml parser
import toml
# Interface import
from interface import SteelEagleInterface
from vr_interface import SteelEagleVrInterface
from colorhash import ColorHash

"""Simulator for kinematic vehicle motion.

Simulates kinematic vehicle motion, either flying or on
the ground. Also has features for logical inference with
mock AI engines and actors which can be configured to
move around the space.
"""

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Set headless mode
loadPrcFileData("", """
  window-type none
""")

class Simulator(ShowBase):
    def __init__(self):
        super().__init__()

        # Lighting
        ambient = AmbientLight("ambient")
        ambient.setColor((0.7, 0.7, 0.7, 1))
        self.render.setLight(self.render.attachNewNode(ambient))

        directional = DirectionalLight("directional")
        directional.setDirection(LVector3f(-1, -1, -2))
        directional.setColor((0.8, 0.8, 0.8, 1))
        self.render.setLight(self.render.attachNewNode(directional))

        # Holds references to all moving entities
        self.actors = []
        self.vehicle_set = set([])
        self.vehicles = []
        self.interfaces = []

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
            raise RuntimeError("Failed to create a GraphicsPipe (no GLX/EGL/etc available?)")

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
            "MainHost",
            0,
            fbp,
            wp,
            GraphicsPipe.BFRefuseWindow,
            None,
            None
        )

        # Simulation tick task
        self.taskMgr.add(self.simulate, "simulate-task")

    def add_vehicle(self, name: str, origin: GeodeticPoint, ground_size: int = 100, engines: Optional[dict] = {}, **kwargs) -> Vehicle:
        """Add a moveable vehicle to the simulation.

        Adds a vehicle and an actuation interface (SteelEagle) to the
        simulation. Motion is kinematic so there is no collision detection.

        Args:
            name (str): name of the vehicle (must be unique)
            origin (GeodeticPoint): origin geodetic point of the vehicle
            engines (dict): dictionary of engines to attach to this vehicle,
                default None

        Returns:
            Vehicle: newly created vehicle object
        """
        # Check if name already exists, in this case ignore!
        if name in self.vehicle_set:
            return False
        else:
            self.vehicle_set.add(name)

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
        cm = CardMaker(f"Ground [{name}]")
        cm.setFrame(-ground_size, ground_size, -ground_size, ground_size)
        ground = self.render.attachNewNode(cm.generate())
        ground.setHpr(0, -90, 0)  # horizontal X-Y plane
        tex = self.loader.load_texture("maps/envir-ground.jpg")
        tex.set_wrap_u(Texture.WM_repeat)
        tex.set_wrap_v(Texture.WM_repeat)
        ground.set_texture(tex)
        ts = TextureStage.get_default()
        ground.set_tex_scale(ts, 20, 20)  # Repeat 4x4 times
        ground.setBin("fixed", 0)
        ground_holder = GroundHolder(ground, ts)

        # Set up lens according to camera intrinsics
        lens = PerspectiveLens()
        lens.set_film_size(*size)
        lens.set_fov(*fov)
        lens.setNear(0.1)
        lens.setFar(1000.0)
        camera = self.makeCamera(buffer, lens=lens, camName=f'Image Camera [{name}]')
        proj = camera.attachNewNode('proj-holder')

        camera.reparentTo(self.render)
        model = self.loader.loadModel("models/misc/objectHandles")
        model.setScale(0.5, 0.5, 0.5)
        model.setBin("fixed", 10)
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
        text_nodepath.setPos(0,0,1)
        text_nodepath.setScale(0.5)
        text_nodepath.setBin("fixed", 10)
        camera_holder = CameraHolder(camera, buffer, texture, proj, fov, size)

        # Set the simulation anchor if it hasn't already been set
        if not self.anchor:
            self.anchor = origin

        vehicle = Vehicle(name, origin, self.anchor, camera_holder, ground_holder)
        # Build sink objects and attach them to the interface, if they exist
        engine_holder = None
        if len(engines):
            engine_objects = []
            for e in engines:
                engine_objects.append(get_engine_from_name(e, **engines[e]))
            engine_holder = EngineHolder(vehicle, self.actors, engine_objects)
        # Create and store a reference to the control interface
        # TODO: Add interface using: self.interfaces.append()
        # Store a reference to this vehicle
        self.vehicles.append(vehicle)

        logger.info(f'Added vehicle {name} at point {origin}!')
        return vehicle

    def add_actor(self, name: str, tag: str, origin: GeodeticPoint, **kwargs) -> Actor:
        """Add a moveable actor to the simulation.

        Adds an actor to the simulation. Motion is kinematic so there
        is no collision detection. Actors are generally modelled as
        white cubes.

        Args:
            name (str): name of the actor
            tag (str): type of object to be reported by object detector logical inference
            origin (GeodeticPoint): origin geodetic point of the actor

        Returns:
            Actor: newly created actor object
        """

        parent = NodePath(PandaNode(name))
        parent.reparentTo(self.render)
        cube = self.loader.loadModel("models/box")

        obj_len = kwargs.get('length', 1)
        obj_width = kwargs.get('width', 1)
        obj_height = kwargs.get('height', 1)

        cube.setScale(obj_len, obj_width, obj_height) # Defaults to 1x1x1m cube if not specified in config file
        cube.reparentTo(parent)
        cube.setTextureOff(1)  # Disable default texture
        color = ColorHash(tag)
        r,g,b = color.rgb
        cube.setColor(r/255, g/255, b/255, 1)  # Normalize to 0.0-1.0 for Panda3D
        cube.setBin("fixed", 10)
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
        text_nodepath.setBin("fixed", 10)

        # Set the simulation anchor
        if not self.anchor:
            self.anchor = origin

        logger.info(f'Added actor {name} with tag {tag} and color {color.rgb} at point {origin}!')
        logger.info(f'Actor {name} given velocity {kwargs["velocity"]} with waypoint set {kwargs["waypoints"]}')
        actor = Actor(name, tag, origin, self.anchor, parent, **kwargs)
        self.actors.append(actor)
        return actor

    def set_active_trace(self):
        self.active_trace = True

    def end_active_trace(self):
        self.active_trace = False

    def has_active_trace(self):
        return self.active_trace

    def set_trace_name(self, active_trace_name):
        self.trace_name = active_trace_name

    def get_trace_name(self):
        return self.trace_name

    def simulate(self, task):
        """Simulation tick, auto-called by Panda3d."""
        dt = globalClock.getDt()
        # Move objects
        for act in self.actors:
            act.move(dt)
            if self.has_active_trace() and not act.has_waypoints_remaining():
                if 'slalom' in self.get_trace_name():
                    continue
                self.end_active_trace()
                for interface in self.interfaces:
                    interface._interface.attempt_publish_results(self.get_trace_name())

        # Move vehicles
        for veh in self.vehicles:
            veh.move(dt)

        # Build a new frame
        self.graphics_engine.render_frame()

        return task.cont

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Simulates digital twin SteelEagle drones in a configurable 3D world."
    )
    parser.add_argument(
        "--config",
        type=str,
        default="aviary.toml",
        help="override config file path (default: aviary.toml)"
    )
    args = parser.parse_args()

    config = None
    try:
        with open(args.config) as file:
            config = toml.load(file)
    except Exception as e:
        logger.error(f'Failed to load config file {args.config}, reason: {e}')
        quit()

    app = Simulator()

    try:
        for a in config['actor']:
            print(a)
            actor = config['actor'][a]
            tag = actor['tag']
            origin = GeodeticPoint(actor['lat'], actor['lon'], 0)
            kwargs = actor['kwargs']
            kwargs['waypoints'] = actor.get('waypoints', [])
            kwargs['velocity'] = actor.get('velocity', 0)
            print(kwargs)
            print(actor)
            app.add_actor(a, tag, origin, **kwargs)

        for v in config['vehicle']:
            vehicle = config['vehicle'][v]
            origin = GeodeticPoint(vehicle['lat'], vehicle['lon'], 0)
            engines = vehicle['engines']
            kwargs = vehicle['kwargs']
            app.add_vehicle(v, origin, engines=engines, **kwargs)
            logger.info(kwargs)
            if 'vr_interface' in kwargs and kwargs['vr_interface']:
                logger.info(f'Active trace registered for vr interface - {args.config}')
                app.set_active_trace()
                app.set_trace_name(args.config)

    except Exception as e:
        logger.error(f'Failed to add objects, reason: {e}')
        quit()

    logger.info('Simulation started! Connect with a vehicle to get started.')
    app.run()
