from enum import Enum
from dataclasses import dataclass
from panda3d.core import NodePath, TextureStage, GraphicsOutput, Texture

@dataclass
class GroundHolder:
    ground: NodePath
    texture_stage: TextureStage

@dataclass
class CameraHolder:
    camera: NodePath
    buffer: GraphicsOutput
    texture: Texture
    proj: NodePath
    fov: int
    size: int

class Mode(Enum):
    POSITION = 'POSITION'
    VELOCITY = 'VELOCITY'
    WAYPOINT = 'WAYPOINT'

class PoseMode(Enum):
    ANGLE = 'ANGLE'
    OFFSET = 'OFFSET'
    VELOCITY = 'VELOCITY'

@dataclass
class GeodeticPoint:
    latitude: float
    longitude: float
    altitude: float

