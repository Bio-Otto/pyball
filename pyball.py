#!/usr/bin/env python
# -*- coding: utf-8 -*-

import numpy as np

from vispy import gloo, scene
from vispy import app
from vispy.util.transforms import perspective, translate, rotate
from vispy.scene.visuals import Text

from pprint import pprint
import math
import sys

import numpy as np
import numpy.linalg as linalg
import itertools

import render
from spacehash import SpaceHash

import OpenGL.GL as gl

from ctypes import c_float

from pdbremix import pdbatoms
from pdbremix import v3numpy as v3
from pdbremix.data import backbone_atoms


#########################################################
# Convert PDB structure into smooth pieces of secondary 
# structure and geometrial objects that use
# render functions to turn into polygon


class Trace:
  def __init__(self, n=None):
    if n is not None:
      self.points = np.zeros((n,3), dtype=np.float32)
      self.ups = np.zeros((n,3), dtype=np.float32)
      self.tangents = np.zeros((n,3), dtype=np.float32)
      self.objids = np.zeros(n, dtype=np.float32)
      self.residues = [None for i in xrange(n)]

  def get_prev_point(self, i):
    if i > 0:
      return self.points[i-1]
    else:
      return self.points[i] - self.tangents[i]

  def get_next_point(self, i):
    if i < len(self.points)-1:
      return self.points[i+1]
    else:
      return self.points[i] + self.tangents[i]

  def get_prev_up(self, i):
    if i > 0:
      return self.ups[i-1]
    else:
      return self.ups[i]

  def get_next_up(self, i):
    if i < len(self.points)-1:
      return self.ups[i+1]
    else:
      return self.ups[i]


class SubTrace(Trace):
  def __init__(self, trace, i, j):
    self.points = trace.points[i:j]
    self.ups = trace.ups[i:j]
    self.tangents = trace.tangents[i:j]
    self.objids = trace.objids[i:j]
    self.residues = trace.residues[i:j]


def catmull_rom_spline(t, p1, p2, p3, p4):
  """
  Returns a point at fraction t between p2 and p3.
  """
  return \
      0.5 * (   t*((2-t)*t    - 1)  * p1
              + (t*t*(3*t - 5) + 2) * p2
              + t*((4 - 3*t)*t + 1) * p3
              + (t-1)*t*t           * p4 )


class SplineTrace(Trace):
  """
  SplineTrace expands the points in a Trace using
  a spline interpolation.
  """
  def __init__(self, trace, n_division):
    Trace.__init__(self, n_division*(len(trace.points)-1) + 1)

    delta = 1/float(n_division)

    offset = 0
    n_trace_point = len(trace.points)
    for i in range(n_trace_point - 1):
      n = n_division
      j = i+1
      # last division includes the very last trace point
      if j == n_trace_point - 1:
        n += 1
      for k in range(n):
        l = offset + k
        self.points[l,:] = catmull_rom_spline(
            k*delta, 
            trace.get_prev_point(i), 
            trace.points[i],
            trace.points[j], 
            trace.get_next_point(j))
        self.ups[l,:] = catmull_rom_spline(
             k*delta, 
             trace.get_prev_up(i), 
             trace.ups[i], 
             trace.ups[j], 
             trace.get_next_up(j))
        if k/float(n) < 0.5:
          self.objids[l] = trace.objids[i]
        else:
          self.objids[l] = trace.objids[i+1]
      offset += n

    n_point = len(self.points)
    for i in range(n_point):
      if i == 0:
        tangent = trace.tangents[0]
      elif i == n_point-1:
        tangent = trace.tangents[-1]
      else:
        tangent = self.points[i+1] - self.points[i-1]
      self.tangents[i,:] = tangent


class Bond():
  def __init__(self, atom1, atom2):
    self.atom1 = atom1
    self.atom2 = atom2


class RenderedSoup():
  def __init__(self, soup):
    self.soup = soup

    self.atom_by_objid = {}
    self.build_objids()

    self.build_trace()

    self.bonds = []
    self.find_bonds()

    self.pieces = []
    self.find_pieces()

    # self.find_ss_by_zhang_skolnick()
    self.find_bb_hbonds()
    self.find_ss_by_bb_hbonds()

  def build_objids(self):
    for i_atom, atom in enumerate(self.soup.atoms()):
      self.atom_by_objid[i_atom] = atom
      atom.objid = i_atom

  def build_trace(self):
    trace_residues = []
    for residue in self.soup.residues():
      residue.ss = '-'
      residue.color = [0.4, 1.0, 0.4]
      if residue.has_atom('CA') and residue.has_atom('C') and residue.has_atom('O'):
        ca = residue.atom('CA')
        trace_residues.append(residue)
        res_objid = ca.objid
      else:
        res_objid = residue.atoms()[0].objid
      residue.objid = res_objid
      for atom in residue.atoms():
        atom.residue = residue

    self.trace = Trace(len(trace_residues))
    for i, residue in enumerate(trace_residues):
      ca = residue.atom('CA')
      c = residue.atom('C')
      o = residue.atom('O')
      residue.i = i
      self.trace.residues[i] = residue
      self.trace.objids[i] = residue.objid
      self.trace.points[i] = ca.pos
      self.trace.ups[i] = c.pos - o.pos

    # remove alternate conformation by looking for orphaned atoms
    atoms = self.soup.atoms()
    n = len(atoms)
    for i in reversed(range(n)):
      atom = atoms[i]
      if not hasattr(atom, 'residue'):
        del atoms[i]

    # make ups point in the same direction
    for i in range(1, len(self.trace.points)):
      if v3.dot(self.trace.ups[i-1], self.trace.ups[i]) < 0:
         self.trace.ups[i] = -self.trace.ups[i]

    # find geometrical center of points
    self.center = v3.get_center(self.trace.points)
    centered_points = [p - self.center for p in self.trace.points]

    self.scale = 1.0/max(map(max, centered_points))

  def find_bb_hbonds(self):
    print "Find H-Bonds..."
    vertices = []
    atoms = []
    for residue in self.trace.residues:
      if residue.has_atom('O'):
        atom = residue.atom('O')  
        atoms.append(atom)
        vertices.append(atom.pos)
      if residue.has_atom('N'):
        atom = residue.atom('N')  
        atoms.append(atom)
        vertices.append(atom.pos)
      residue.hb_partners = []
    d = 3.5
    for i, j in SpaceHash(vertices).close_pairs():
      atom1 = atoms[i]
      atom2 = atoms[j]
      if atom1.type == atom2.type:
        continue
      if v3.distance(atom1.pos, atom2.pos) < d:
        res1 = atom1.residue
        res2 = atom2.residue
        res1.hb_partners.append(res2.i)
        res2.hb_partners.append(res1.i)

  def find_ss_by_bb_hbonds(self):

    def is_hb(i_res, j_res):
      if not (0 <= i_res <= len(self.trace.residues) - 1):
        return False
      return j_res in self.trace.residues[i_res].hb_partners

    print "Find Secondary Structure..."
    for res in self.trace.residues:
      res.ss = 'C'

    n_res = len(self.trace.residues)
    for i_res1 in range(n_res):

      # alpha-helix
      if is_hb(i_res1, i_res1+4) and is_hb(i_res1+1, i_res1+5):
        for i_res in range(i_res1+1, i_res1+5):
          self.trace.residues[i_res].ss = 'H'

      # 3-10 helix
      if is_hb(i_res1, i_res1+3) and is_hb(i_res1+1, i_res1+4):
        for i_res in range(i_res1+1, i_res1+4):
          self.trace.residues[i_res].ss = 'H'

      for i_res2 in range(n_res):
        if abs(i_res1-i_res2) > 5:
          if is_hb(i_res1, i_res2):
            beta_residues = []

            # parallel beta sheet pairs
            if is_hb(i_res1-2, i_res2-2):
              beta_residues.extend(
                  [i_res1-2, i_res1-1, i_res1, i_res2-2, i_res2-1, i_res2])
            if is_hb(i_res1+2, i_res2+2):
              beta_residues.extend(
                  [i_res1+2, i_res1+1, i_res1, i_res2+2, i_res2+1, i_res2])

            # anti-parallel beta sheet pairs
            if is_hb(i_res1-2, i_res2+2):
              beta_residues.extend(
                  [i_res1-2, i_res1-1, i_res1, i_res2+2, i_res2+1, i_res2])
            if is_hb(i_res1+2, i_res2-2):
              beta_residues.extend(
                  [i_res1+2, i_res1+1, i_res1, i_res2-2, i_res2-1, i_res2])

            for i_res in beta_residues:
              self.trace.residues[i_res].ss = 'E' 

    color_by_ss = {
      '-': (0.5, 0.5, 0.5),
      'C': (0.5, 0.5, 0.5),
      'H': (0.8, 0.4, 0.4),
      'E': (0.4, 0.4, 0.8)
    }
    for residue in self.trace.residues:
      residue.color = color_by_ss[residue.ss]

  def find_pieces(self, cutoff=5.5):

    self.pieces = []

    i = 0
    n_point = len(self.trace.points)

    for j in range(1, n_point+1):
      is_new_piece = False
      if j == n_point:
        is_new_piece = True
      else:
        dist = v3.distance(self.trace.points[j-1], self.trace.points[j]) 
        if dist > cutoff:
          is_new_piece = True

      if is_new_piece:
        for k in range(i, j):
          if k == i:
            tangent = self.trace.points[i+1] - self.trace.points[i]
          elif k == j-1:
            tangent = self.trace.points[k] - self.trace.points[k-1]
          else:
            tangent = self.trace.points[k+1] - self.trace.points[k-1]
          self.trace.tangents[k] = v3.norm(tangent)

        ups = []
        # smooth then rotate
        for k in range(i, j):
          up = self.trace.ups[k]
          if k > i:
            up = up + self.trace.ups[k-1]
          elif k < j-1:
            up = up + self.trace.ups[k+1]
          ups.append(v3.norm(v3.perpendicular(up, self.trace.tangents[k])))
        self.trace.ups[i:j] = ups

        self.pieces.append(SubTrace(self.trace, i, j))

        i = j

  def find_bonds(self):
    self.draw_to_screen_atoms = self.soup.atoms()
    backbone_atoms.remove('CA')
    self.draw_to_screen_atoms = [a for a in self.draw_to_screen_atoms if a.type not in backbone_atoms and a.element!="H"]
    vertices = [a.pos for a in self.draw_to_screen_atoms]
    self.bonds = []
    print "Finding bonds..."
    for i, j in SpaceHash(vertices).close_pairs():
      atom1 = self.draw_to_screen_atoms[i]
      atom2 = self.draw_to_screen_atoms[j]
      d = 2
      if atom1.element == 'H' or atom2.element == 'H':
        continue
      if v3.distance(atom1.pos, atom2.pos) < d:
        if atom1.alt_conform != " " and atom2.alt_conform != " ":
          if atom1.alt_conform != atom2.alt_conform:
            continue
        bond = Bond(atom1, atom2)
        bond.tangent = atom2.pos - atom1.pos
        bond.up = v3.cross(atom1.pos, bond.tangent)
        self.bonds.append(bond)



def identity():
  return np.eye(4, dtype=np.float32)


class Camera():
  def __init__(self):
    self.view = identity()
    self.model = identity()
    self.rotation = identity()
    self.projection = identity()
    self.zoom = 40
    self.center = (0, 0, 0, 0)
    translate(self.view, 0, 0, -self.zoom)
    self.is_fog = True
    self.fog_near = -1
    self.fog_far = 50
    self.fog_color = [0, 0, 0]

  def recalc_projection(self):
    self.projection = perspective(
        25.0, 
        self.width / float(self.height), 
        1.0, 
        50.0+self.zoom)
    self.fog_near = self.zoom
    self.fog_far = 20.0 + self.zoom

  def resize(self, width, height):
    self.width, self.height = width, height
    self.size = [width, height]
    self.recalc_projection()

  def recalc_model(self):
    self.model = np.dot(self.translation, self.rotation)
      
  def rotate(self, phi_diff, theta_diff, psi_diff):
    rotate(self.rotation, phi_diff, 0, 1, 0)
    rotate(self.rotation, theta_diff, 1, 0, 0)
    rotate(self.rotation, psi_diff, 0, 0, -1)
    self.recalc_model()

  def rezoom(self, zoom_diff):
    self.zoom = max(10, self.zoom + zoom_diff)
    self.view = identity()
    translate(self.view, 0, 0, -self.zoom)
    self.recalc_projection()

  def set_center(self, center):
    self.center = center
    self.translation = identity()
    translate(self.translation, -self.center[0], -self.center[1], -self.center[2])
    self.recalc_model()



class TriangleStore:
  def __init__(self, n_vertex):
    self.data = np.zeros(
      n_vertex, 
      [('a_position', np.float32, 3),
       ('a_normal', np.float32, 3),
       ('a_color', np.float32, 3),
       ('a_objid', np.float32, 1)])
    self.i_vertex = 0
    self.n_vertex = n_vertex
    self.indices = []

  def add_vertex(self, vertex, normal, color, objid):
    self.data['a_position'][self.i_vertex,:] = vertex
    self.data['a_normal'][self.i_vertex,:] = normal
    self.data['a_color'][self.i_vertex,:] = color
    self.data['a_objid'][self.i_vertex] = objid
    self.i_vertex += 1

  def vertex_buffer(self):
    return gloo.VertexBuffer(self.data) 
  
  def index_buffer(self):
    return gloo.IndexBuffer(self.indices) 

  def setup_next_strip(self, indices):
    """
    Add triangular indices relative to self.i_vertex_in_buffer
    """
    indices = [i + self.i_vertex for i in indices]
    self.indices.extend(indices)


def group(lst, n):
    """
    Returns iterable of n-tuple from a list.Incomplete tuples discarded 
    http://code.activestate.com/recipes/303060-group-a-list-into-sequential-n-tuples/
    >>> list(group(range(10), 3))
        [(0, 1, 2), (3, 4, 5), (6, 7, 8)]
    """
    return itertools.izip(*[itertools.islice(lst, i, None, n) for i in range(n)])



def make_calpha_arrow_mesh(
    trace, length=0.7, width=0.35, thickness=0.3):
  arrow = render.Arrow(length, width, thickness)

  n_point = len(trace.points)
  triangle_store = TriangleStore(n_point*len(arrow.indices))

  for i_point in range(n_point):

    orientate = arrow.get_orientate(
        trace.tangents[i_point], trace.ups[i_point], 1.0)

    for indices in group(arrow.indices, 3):

      points = [arrow.vertices[i] for i in indices]

      normal = v3.cross(points[1] - points[0], points[0] - points[2])
      normal = v3.transform(orientate, normal)

      for point in points:
        triangle_store.add_vertex(
          v3.transform(orientate, point) + trace.points[i_point],
          normal, 
          trace.residues[i_point].color, 
          trace.objids[i_point])

  return triangle_store.vertex_buffer()



def make_cylinder_trace_mesh(pieces, coil_detail=4, radius=0.3):
  cylinder = render.Cylinder(coil_detail)

  n_point = sum(len(piece.points) for piece in pieces)
  triangle_store = TriangleStore(2 * n_point * cylinder.n_vertex)

  for piece in pieces:
    points = piece.points

    for i_point in xrange(len(points) - 1):

      tangent = 0.5*(points[i_point+1] - points[i_point])

      up = piece.ups[i_point] + piece.ups[i_point+1]

      orientate = cylinder.get_orientate(tangent, up, radius)
      triangle_store.setup_next_strip(cylinder.indices)
      for point, normal in zip(cylinder.points, cylinder.normals):
        triangle_store.add_vertex(
            v3.transform(orientate, point) + points[i_point],
            v3.transform(orientate, normal), 
            piece.residues[i_point].color, 
            piece.objids[i_point])

      orientate = cylinder.get_orientate(-tangent, up, radius)
      triangle_store.setup_next_strip(cylinder.indices)
      for point, normal in zip(cylinder.points, cylinder.normals):
        triangle_store.add_vertex(
            v3.transform(orientate, point) + points[i_point+1],
            v3.transform(orientate, normal), 
            piece.residues[i_point+1].color, 
            piece.objids[i_point+1])

  return triangle_store.index_buffer(), triangle_store.vertex_buffer()


def make_carton_mesh(
    pieces, coil_detail=5, spline_detail=3, 
    width=1.6, thickness=0.2):

  rect = render.RectProfile(width, 0.15)
  circle = render.CircleProfile(coil_detail, 0.3)

  builders = []
  for piece in pieces:
    spline = SplineTrace(piece, 2*spline_detail)

    n_point = len(piece.points)

    i_point = 0
    j_point = 1
    while i_point < n_point:

      ss = piece.residues[i_point].ss
      color = piece.residues[i_point].color
      color = [min(1.0, 1.2*c) for c in color]
      profile = circle if ss == "C" else rect  

      while j_point < n_point and piece.residues[j_point].ss == ss:
        j_point += 1

      i_spline = 2*i_point*spline_detail - spline_detail
      if i_spline < 0:
        i_spline = 0
      j_spline = (j_point-1) * 2*spline_detail + spline_detail + 1
      if j_spline > len(spline.points) - 1:
        j_spline = len(spline.points) - 1

      sub_spline = SubTrace(spline, i_spline, j_spline)

      builders.append(render.TubeBuilder(sub_spline, profile, color))

      i_point = j_point
      j_point = i_point + 1

  n_vertex = sum(r.n_vertex for r in builders)
  triangle_store = TriangleStore(n_vertex)

  for r in builders:
      r.build_triangles(triangle_store)

  return triangle_store.index_buffer(), triangle_store.vertex_buffer()



def make_ball_and_stick_mesh(
    rendered_soup, sphere_stack=5, sphere_arc=5, 
    tube_arc=5, radius=0.2):

  sphere = render.Sphere(sphere_stack, sphere_arc)
  cylinder = render.Cylinder(4)

  n_vertex = len(rendered_soup.draw_to_screen_atoms)*sphere.n_vertex
  n_vertex += 2*len(rendered_soup.bonds)*cylinder.n_vertex
  triangle_store = TriangleStore(n_vertex)

  for atom in rendered_soup.draw_to_screen_atoms:
    triangle_store.setup_next_strip(sphere.indices)
    orientate = sphere.get_orientate(radius)
    for point in sphere.points:
      triangle_store.add_vertex(
          v3.transform(orientate, point) + atom.pos,
          point, # same as normal!
          atom.residue.color, 
          atom.objid)

  for bond in rendered_soup.bonds:
    tangent = 0.5*bond.tangent

    orientate = cylinder.get_orientate(tangent, bond.up, radius)
    triangle_store.setup_next_strip(cylinder.indices)
    for point, normal in zip(cylinder.points, cylinder.normals):
      triangle_store.add_vertex(
          v3.transform(orientate, point) + bond.atom1.pos,
          v3.transform(orientate, normal), 
          bond.atom1.residue.color, 
          bond.atom1.objid)

    orientate = cylinder.get_orientate(-tangent, bond.up, radius)
    triangle_store.setup_next_strip(cylinder.indices)
    for point, normal in zip(cylinder.points, cylinder.normals):
      triangle_store.add_vertex(
          v3.transform(orientate, point) + bond.atom2.pos,
          v3.transform(orientate, normal), 
          bond.atom2.residue.color, 
          bond.atom2.objid)

  return triangle_store.index_buffer(), triangle_store.vertex_buffer()



semilight_vertex = """
uniform mat4 u_model;
uniform mat4 u_normal;
uniform mat4 u_view;
uniform mat4 u_projection;

attribute vec3  a_position;
attribute vec3  a_normal;
attribute vec3  a_color;
attribute float a_objid;

varying vec4 N;

void main (void)
{
  gl_Position = u_projection * u_view * u_model * vec4(a_position, 1.0);
  N = normalize(u_normal * vec4(a_normal, 1.0));
  gl_FrontColor = vec4(a_color, 1.);
}
"""



semilight_fragment = """
uniform bool u_is_lighting;
uniform vec3 u_light_position;
uniform bool u_is_fog;
uniform float u_fog_near;
uniform float u_fog_far;
uniform vec3 u_fog_color;

const vec4 ambient_color = vec4(.2, .2, .2, 1.);
const vec4 diffuse_intensity = vec4(1., 1., 1., 1.); 

varying vec4 N;

void main()
{
  if (u_is_lighting) {
    vec4 color = gl_Color;
    vec4 L = vec4(normalize(u_light_position.xyz), 1);
    vec4 ambient = color * ambient_color;
    vec4 diffuse = color * diffuse_intensity;
    float d = max(0., dot(N, L));
    color = clamp(ambient + diffuse * d, 0., 1.);
    gl_FragColor = color;
  }

  if (u_is_fog) {
    float depth = gl_FragCoord.z / gl_FragCoord.w;
    float fog_factor = smoothstep(u_fog_near, u_fog_far, depth);
    gl_FragColor = mix(
        gl_FragColor, 
        vec4(u_fog_color, gl_FragColor.w), 
        fog_factor);
  }
}
"""



picking_vertex = """

uniform mat4 u_model;
uniform mat4 u_normal;
uniform mat4 u_view;
uniform mat4 u_projection;

attribute vec3 a_position;
attribute vec3 a_normal;
attribute vec3 a_color;
attribute float a_objid;

varying float objid;

void main(void) {
  gl_Position = u_projection * u_view * u_model * vec4(a_position, 1.0);
  objid = a_objid;
}
"""



picking_fragment = """

varying float objid;

int int_mod(int x, int y) { 
  int z = x / y;
  return x - y*z;
}

void main(void) {
  // ints are only required to be 7bit...
  int int_objid = int(objid + 0.5);
  int red = int_mod(int_objid, 256);
  int_objid /= 256;
  int green = int_mod(int_objid, 256);
  int_objid /= 256;
  int blue = int_mod(int_objid, 256);
  gl_FragColor = vec4(float(red), float(green), float(blue), 255.0)/255.0;
}

"""


def get_polar(x, y):
  r = math.sqrt(x*x + y*y)
  if x != 0.0:
    theta = math.atan(y/float(x))
  else:
    if y > 0:
      theta = math.pi/2
    else:
      theta = -math.pi/2
  if x<0:
    if y>0:
      theta += math.pi
    else:
      theta -= math.pi
  return r, theta



class Console():
  def __init__(self, size, init_str=''):
    self.text = Text(
          init_str, bold=True, color=(0.7, 1.0, 0.3, 1.),
          font_size=10, pos=(0, 0), anchor_y='bottom',
          anchor_x='center')
    self.size = size
    self.x = 0
    self.y = 0

  def draw(self):
      viewport = gloo.get_parameter('viewport')
      size = viewport[2:4]
      x_view_offset = (size[0] - self.size[0]) // 2
      y_view_offset = (size[1] - self.size[1]) // 2
      x =  self.x +      x_view_offset
      y =  self.y + 15 + y_view_offset
      gloo.set_viewport(x, y, self.size[0], self.size[1])
      self.text.pos = (0, 0)
      self.text.draw()


class MolecularViewerCanvas(app.Canvas):

    def __init__(self, fname):
      app.Canvas.__init__(
          self, title='Molecular viewer')

      # self.size is not updated until after __init__ is 
      # finished so must use the local `size` variable during
      # __init__
      size = 500, 300
      self.size = size
      gloo.set_viewport(0, 0, size[0], size[1])

      self.program = gloo.Program(semilight_vertex, semilight_fragment)
      self.picking_program = gloo.Program(picking_vertex, picking_fragment)

      soup = pdbatoms.Soup(fname)

      rendered_soup = RenderedSoup(soup)
      self.rendered_soup = rendered_soup

      print "Building arrows..."
      self.arrow_buffer = make_calpha_arrow_mesh(rendered_soup.trace)

      print "Building cylindrical trace..."
      self.cylinder_index_buffer, self.cylinder_vertex_buffer = \
          make_cylinder_trace_mesh(rendered_soup.pieces)

      print "Building cartoon..."
      self.cartoon_index_buffer, self.cartoon_vertex_buffer = \
          make_carton_mesh(rendered_soup.pieces)

      print "Building ball&sticks..."
      self.ballstick_index_buffer, self.ballstick_vertex_buffer = \
          make_ball_and_stick_mesh(rendered_soup)

      self.draw_style = 'sidechains'

      self.camera = Camera()
      self.camera.resize(*size)
      self.camera.set_center(rendered_soup.center)
      self.camera.rezoom(2.0/rendered_soup.scale)

      self.new_camera = Camera()
      self.n_step_animate = 0 

      self.console = Console(size)
      self.text = self.console.text

      self.timer = app.Timer(1.0 / 30)  # change rendering speed here
      self.timer.connect(self.on_timer)
      self.timer.start()

    def on_initialize(self, event):
      gloo.set_state(depth_test=True, clear_color='black')

    def draw_buffers(self, program):
      if self.draw_style == 'sidechains':
        program.bind(self.ballstick_vertex_buffer)
        program.draw('triangles', self.ballstick_index_buffer)

      program.bind(self.arrow_buffer)
      program.draw('triangles')

      program.bind(self.cartoon_vertex_buffer)
      program.draw('triangles', self.cartoon_index_buffer)

    def on_draw(self, event):
      gloo.clear()
      gloo.set_viewport(0, 0, self.camera.width, self.camera.height)

      self.program['u_light_position'] = [100., 100., 500.]
      self.program['u_is_lighting'] = True
      self.program['u_model'] = self.camera.model
      self.program['u_normal'] = self.camera.rotation 
      self.program['u_view'] = self.camera.view
      self.program['u_projection'] = self.camera.projection
      self.program['u_is_fog'] = self.camera.is_fog
      self.program['u_fog_far'] = self.camera.fog_far
      self.program['u_fog_near'] = self.camera.fog_near
      self.program['u_fog_color'] = self.camera.fog_color

      gl.glEnable(gl.GL_BLEND)
      gl.glEnable(gl.GL_DEPTH_TEST)
      gl.glDepthFunc(gl.GL_LEQUAL)
      gl.glCullFace(gl.GL_FRONT)
      gl.glEnable(gl.GL_CULL_FACE)

      self.draw_buffers(self.program)

      gl.glDisable(gl.GL_BLEND)
      gl.glDisable(gl.GL_DEPTH_TEST)
      gl.glDisable(gl.GL_CULL_FACE)

      self.console.draw()

      self.last_draw = 'screen'

      self.update()

    def pick_draw(self):
      gloo.set_viewport(0, 0, self.camera.width, self.camera.height)
      gl.glDisable(gl.GL_BLEND)
      gl.glEnable(gl.GL_DEPTH_TEST)
      gl.glDepthFunc(gl.GL_LEQUAL)
      gl.glCullFace(gl.GL_FRONT)
      gl.glEnable(gl.GL_CULL_FACE)

      gl.glClearColor(0.0, 0.0, 0.0, 0.0)
      gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)

      self.picking_program['u_model'] = self.camera.model
      self.picking_program['u_view'] = self.camera.view
      self.picking_program['u_projection'] = self.camera.projection

      self.draw_buffers(self.picking_program)

      self.last_draw = 'pick'

    def pick(self, x, y):
      if self.last_draw != 'pick':
        self.pick_draw()

      pixels = (c_float*4)()
      y_screen = self.size[1] - y # screen and OpenGL y coord flipped
      gl.glReadPixels(x, y_screen, 1, 1, gl.GL_RGBA, gl.GL_FLOAT, pixels)
      
      return int(pixels[2]*255*256*256) + \
             int(pixels[1]*255*256) + \
             int(pixels[0]*255)

    def on_key_press(self, event):
      if event.text == ' ':
        if self.timer.running:
          self.timer.stop()
        else:
          self.timer.start()
      if event.text == 's':
        if self.draw_style == 'sidechains':
          self.draw_style = 'no-sidechains'
        else:
          self.draw_style = 'sidechains'

    def on_timer(self, event):
      if self.n_step_animate > 0:
        diff = self.new_camera.center - self.camera.center
        fraction = 1.0/float(self.n_step_animate)
        new_center = self.camera.center + fraction*diff
        self.camera.set_center(new_center)
        self.n_step_animate -= 1
        self.update()

    def on_resize(self, event):
      self.camera.resize(*event.size)

    def on_mouse_press(self, event):
      self.save_event = event
      self.save_objid = self.pick(*event.pos)

    def on_mouse_release(self, event):
      objid = self.pick(*event.pos)
      if self.save_objid == objid and objid > 0:
        atom = self.rendered_soup.atom_by_objid[objid]
        self.new_camera.center = atom.pos
        self.n_step_animate = 10

    def on_mouse_move(self, event):
      objid = self.pick(*event.pos)
      if objid <= 0:
        self.console.text.text = ''
      if objid > 0:
        atom = self.rendered_soup.atom_by_objid[objid]
        s = "%s-%s-%s" % (atom.res_tag(), atom.res_type, atom.type)
        self.console.text.text = s
        pos = np.append(atom.pos[:], [1], 0)
        pos = np.dot(pos, self.camera.model)
        pos = np.dot(pos, self.camera.view)
        pos = np.dot(pos, self.camera.projection)
        pos = pos/pos[3]
        self.console.x = pos[0]*self.size[0]*0.5
        self.console.y = pos[1]*self.size[1]*0.5

      if event.button == 1:
        x_diff = event.pos[0] - self.save_event.pos[0]
        y_diff = event.pos[1] - self.save_event.pos[1]
        scale = self.rendered_soup.scale
        self.camera.rotate(
            x_diff/float(self.camera.width)*10/scale, 
            y_diff/float(self.camera.height)*10/scale, 
            0.0)
        self.save_event = event
        self.update()
      elif event.button == 2:
        def get_event_polar(event):
          return get_polar(
              event.pos[0]/float(self.size[0])-0.5, 
              event.pos[1]/float(self.size[1])-0.5)
        r, psi = get_event_polar(event)
        r0, psi0 = get_event_polar(self.save_event)
        self.camera.rezoom((r0-r)*500.)
        self.camera.rotate(0, 0, (psi - psi0)/math.pi*180)
        self.save_event = event
        self.update()



def main(fname):
    mvc = MolecularViewerCanvas(fname)
    mvc.show()
    app.run()



if __name__ == '__main__':
  if len(sys.argv) < 2:
    print 'Usage: pyball.py pdb'
  else:
    main(sys.argv[1])
