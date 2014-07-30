#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
pyball - a pure-Python protein viewer in OpenGL ES 2.0 subset, 
must provide own shaders, matrices and lighting.

This implements a new ribbon representation with arrows at
every Calpha position.

Copyright (c) 2014, Bosco Ho

With bits adapted from Renaud Blanch's PyOpenGl tutorials
& Marco Biasini's javascript PV viewer.
"""


import os
import sys
from math import exp
from pprint import pprint
import time

from ctypes import sizeof, c_float, c_void_p, c_uint, string_at

from OpenGL.GLUT import *
from OpenGL.GL import *

from pdbremix import pdbatoms
import pdbremix.v3numpy as v3
from pdbremix.data import backbone_atoms

import png
import camera
import shader
import render
from spacehash import SpaceHash



#########################################################
# Convert PDB structure into smooth pieces of secondary 
# structure and geometrial objects that use
# render functions to turn into polygon


class PieceCalphaTrace:
  def __init__(self):
    self.points = []
    self.ups = []
    self.tangents = []
    self.ss = []
    self.objids = []


class SsPieceCalphaTrace:
  def __init__(self, parent, i, j, ss):
    self.parent = parent
    self.i = i
    self.j = j
    self.points = parent.points[i:j] 
    self.objids = parent.objids[i:j] 
    self.ups = parent.ups[i:j] 
    self.tangents = parent.tangents[i:j]
    self.ss = ss

  def get_prev_point(self, i):
    i_parent = self.i + i
    if i_parent == 0:
      prev_point = self.prev_point_save
    else:
      prev_point = self.parent.points[i_parent - 1]
    return prev_point

  def get_next_point(self, i):
    i_parent = self.i + i
    if i_parent == len(self.parent.points)-1:
      next_point = self.next_point_save
    else:
      next_point = self.parent.points[i_parent + 1]
    return next_point

  def get_prev_up(self, i):
    if i == 0 and self.i == 0:
      prev_up = self.parent.ups[0]
    else:
      prev_up = self.parent.ups[self.i + i - 1]
    return prev_up

  def get_next_up(self, i):
    if i == len(self.points)-1 and self.i + i == len(self.parent.points)-1:
      next_up = self.parent.ups[i]
    else:
      next_up = self.parent.ups[self.i + i + 1]
    return next_up


next_objid = 1
def get_next_objid():
  global next_objid
  objid = next_objid
  next_objid += 1
  return objid


def atom_name(atom):
  return atom.res_tag() + '-' + atom.res_type  + '-' + atom.type


class RenderedSoup():
  def __init__(self, soup):
    self.soup = soup
    self.bonds = []

    self.points = []
    self.objids = []
    self.tops = []
    self.ss = []
    self.pieces = []
    self.ss_pieces = []
    self.objid_ref = {}
    self.connected_residues = []

    self.build_objids()
    self.find_points()
    self.find_ss()
    self.find_pieces()
    self.find_ss_pieces()
    self.find_bonds()

  def build_objids(self):
    for atom in self.soup.atoms():
      objid = get_next_objid()
      self.objid_ref[objid] = atom
      atom.objid = objid

  def find_points(self):
    for residue in self.soup.residues():
      if residue.has_atom('CA'):
        self.connected_residues.append(residue)
        atom = residue.atom('CA')
        self.points.append(atom.pos)
        self.tops.append(residue.atom('C').pos - residue.atom('O').pos)
        self.objids.append(atom.objid)
        for atom in residue.atoms():
          atom.res_objid = atom.objid
          atom.residue = residue
    n_point = len(self.points)
    for i in range(1, n_point):
      if v3.dot(self.tops[i-1], self.tops[i]) < 0:
         self.tops[i] = -self.tops[i]
    self.center = v3.get_center(self.points)
    centered_points = [p - self.center for p in self.points]
    self.scale = 1.0/max(map(max, centered_points))

  def find_ss(self):
    
    def zhang_skolnick_test(i, template_dists, delta):
      for j in range(max(0, i-2), i+1):
        for diff in range(2, 5):
          k = j + diff
          if k >= len(self.points):
            continue
          dist = v3.distance(self.points[j], self.points[k])
          if abs(dist - template_dists[diff]) > delta:
            return False
      return True

    helix_distances = { 2:5.45, 3:5.18, 4:6.37 }
    helix_delta = 2.1
    sheet_distances = { 2:6.1, 3:10.4, 4:13.0 }
    sheet_delta = 1.42
    self.ss = []
    for i in range(len(self.points)):
      if zhang_skolnick_test(i, helix_distances, helix_delta):
        self.ss.append('H')
      elif zhang_skolnick_test(i, sheet_distances, sheet_delta):
        self.ss.append('E')
      else:
        self.ss.append('C')
      self.connected_residues[i].ss = self.ss[-1]

  def find_pieces(self):
    self.pieces = []
    i = 0
    n_point = len(self.points)
    for j in range(1, n_point+1):
      is_new_piece = False
      is_break = False
      if j == n_point:
        is_new_piece = True
      else:
        dist = v3.distance(self.points[j-1], self.points[j]) 
        cutoff = 5
        if dist > cutoff:
          is_new_piece = True
      if is_new_piece:
        piece = PieceCalphaTrace()
        piece.points = self.points[i:j]
        piece.objids = self.objids[i:j]
        piece.ss = self.ss[i:j]
        n_point_piece = len(piece.points)
        for k in range(n_point_piece):
          if k == 0:
            tangent = piece.points[1] - piece.points[0]
          elif k == n_point_piece-1:
            tangent = piece.points[k] - piece.points[k-1]
          else:
            tangent = piece.points[k+1] - piece.points[k-1]
          piece.tangents.append(v3.norm(tangent))

        piece.ups = []
        for k in range(n_point_piece):
          k_full = k + i
          up = self.tops[k_full]
          if k > 0:
            up = up + self.tops[k_full-1]
          elif k < n_point_piece-1:
            up = up + self.tops[k_full+1]
          up = v3.perpendicular(up, piece.tangents[k])
          piece.ups.append(v3.norm(up))

        self.pieces.append(piece)
        i = j

  def find_ss_pieces(self):
    self.ss_pieces = []
    for piece in self.pieces:
      ss = piece.ss[0]
      i = 0
      n_point = len(piece.points)
      for j in range(1, n_point+1):
        is_new_piece = False
        if j == n_point:
          is_new_piece = True
        elif piece.ss[j] != ss:
          is_new_piece = True
        if is_new_piece:
          ss_piece = SsPieceCalphaTrace(piece, i, j, ss)
          if i == 0:
            ss_piece.prev_point_save = piece.points[i] - piece.tangents[i]
          else:
            ss_piece.prev_point_save = piece.points[i-1]
          if j == n_point:
            ss_piece.next_point_save = piece.points[j-1] - piece.tangents[j-1]
          else:
            ss_piece.next_point_save = piece.points[j]
          self.ss_pieces.append(ss_piece)
          if j < n_point:
            ss = piece.ss[j]
          i = j-1

  def find_bonds(self):
    self.display_atoms = self.soup.atoms()
    backbone_atoms.remove('CA')
    self.display_atoms = [a for a in self.display_atoms if a.type not in backbone_atoms and a.element!="H"]
    vertices = [a.pos for a in self.display_atoms]
    self.bonds = []
    print "Finding bonds..."
    for i, j in SpaceHash(vertices).close_pairs():
      atom1 = self.display_atoms[i]
      atom2 = self.display_atoms[j]
      d = 2
      if atom1.element == 'H' or atom2.element == 'H':
        continue
      if v3.distance(atom1.pos, atom2.pos) < d:
        bond = Bond(atom1, atom2)
        bond.tangent = atom2.pos - atom1.pos
        bond.up = v3.cross(atom1.pos, bond.tangent)
        self.bonds.append(bond)
    print "Bonds found."


def spline(t, p1, p2, p3, p4):
  """
  Returns a point at fraction t between p2 and p3 
  using Catmull-Rom spline.
  """
  return \
      0.5 * (   t*((2-t)*t    - 1)  * p1
              + (t*t*(3*t - 5) + 2) * p2
              + t*((4 - 3*t)*t + 1) * p3
              + (t-1)*t*t           * p4 )


class SplineTrace():
  """
  A Spline Trace is used to draw ribbons and tubes.
  It's essentially a collection of points, objids, tangents and ups.
  """
  def __init__(self, trace, n_division):
    n_guide_point = len(trace.points)
    delta = 1/float(n_division)

    self.points = []
    self.objids = []
    for i in range(n_guide_point-1):
      n = n_division
      j = i+1
      if j == n_guide_point - 1:
        n += 1
      for k in range(n):
        self.points.append(
          spline(
            k*delta, 
            trace.get_prev_point(i), 
            trace.points[i],
            trace.points[j], 
            trace.get_next_point(j)))
        if k/float(n) < 0.5:
          i_objid = i
        else:
          i_objid = i+1
        self.objids.append(trace.objids[i_objid])

    self.tangents = []
    n_point = len(self.points)
    for i in range(n_point):
      if i == 0:
        tangent = trace.tangents[0]
      elif i == n_point-1:
        tangent = trace.tangents[-1]
      else:
        tangent = self.points[i+1] - self.points[i-1]
      self.tangents.append(tangent)

    self.ups = []
    for i in range(n_guide_point-1):
      if i == 0:
        prev_up = trace.ups[0]
      else:
        prev_up = trace.ups[i-1]
      if i == n_guide_point - 2:
        next_up = trace.ups[-1]
      else:
        next_up = trace.ups[i+2]
      n = n_division
      if i == n_guide_point - 2:
        n += 1
      for k in range(n):
        self.ups.append(
          spline(
             k*delta, 
             trace.get_prev_up(i), 
             trace.ups[i], 
             trace.ups[i+1], 
             trace.get_next_up(i)))


class RibbonTraceRenderer():
  def __init__(self, trace, coil_detail=4, spline_detail=6):
    self.trace = trace
    rect = render.RectProfile()
    circle = render.CircleProfile(coil_detail)
    color_by_ss = {
      'C': (0.8, 0.8, 0.8),
      'H': (1.0, 0.6, 0.6),
      'E': (0.6, 0.6, 1.0)
    }
    self.piece_renderers = []
    for piece in trace.ss_pieces:
      profile = circle if piece.ss == "C" else rect
      color = color_by_ss[piece.ss]
      trace_piece = SplineTrace(piece, spline_detail)
      self.piece_renderers.append(
          render.TubeRender(trace_piece, profile, color))
    
    self.n_vertex = sum(p.n_vertex for p in self.piece_renderers)

  def render_to_buffer(self, vertex_buffer):
    for r in self.piece_renderers:
      r.render_to_buffer(vertex_buffer)


class ShapeTraceRenderer():
  def __init__(self, trace, shape):
    self.trace = trace
    self.shape = shape
    n_point = 0
    for piece in self.trace.ss_pieces:
      n_point += len(piece.points)
    self.n_vertex = shape.n_vertex*n_point

  def render_to_buffer(self, vertex_buffer):
    color_by_ss = {
      'C': (0.5, 0.5, 0.5),
      'H': (1.0, 0.4, 0.4),
      'E': (0.4, 0.4, 1.0)
    }
    for piece in self.trace.ss_pieces:
      n_point = len(piece.points)
      color = color_by_ss[piece.ss]
      for i in range(n_point):
        self.shape.render_to_center(
            vertex_buffer,
            piece.points[i], 
            piece.tangents[i],
            piece.ups[i],
            1.0,
            color,
            piece.objids[i])


def make_carton_mesh(trace, coil_detail=4, spline_detail=6):
  renderers = [
    RibbonTraceRenderer(trace, coil_detail, spline_detail),
    ShapeTraceRenderer(trace, render.ArrowShape(0.4)),
  ]
  n_vertex = sum(p.n_vertex for p in renderers)
  vertex_buffer = render.IndexedVertexBuffer(n_vertex)
  for r in renderers:
    r.render_to_buffer(vertex_buffer)
  return vertex_buffer


class CylinderTraceRenderer():
  def __init__(self, trace, coil_detail):
    self.trace = trace
    n_point = len(self.trace.points)
    self.cylinder = render.CylinderShape(coil_detail)
    self.n_vertex = self.cylinder.n_vertex * (n_point - 1)

  def render_to_buffer(self, vertex_buffer):
    grey = [0.7]*3
    for piece in self.trace.pieces:
      points = piece.points
      for i_segment in range(len(points) - 1):
        tangent = points[i_segment+1] - points[i_segment]
        up = piece.ups[i_segment]
        self.cylinder.render_to_center(
            vertex_buffer,
            points[i_segment],
            tangent,
            up,
            0.5,
            grey,
            piece.objids[i_segment])


def make_cylinder_trace_mesh(
    trace, coil_detail=4, spline_detail=6, sphere_detail=4):
  renderers = [
    CylinderTraceRenderer(trace, coil_detail),
    ShapeTraceRenderer(trace, render.SphereShape(
        sphere_detail, sphere_detail, 0.6)),
  ]
  n_vertex = sum(p.n_vertex for p in renderers)
  vertex_buffer = render.IndexedVertexBuffer(n_vertex)
  for r in renderers:
    r.render_to_buffer(vertex_buffer)
  return vertex_buffer


class Bond():
  def __init__(self, atom1, atom2):
    self.atom1 = atom1
    self.atom2 = atom2


class BallAndStickRenderer():
  def __init__(self, trace):
    self.trace = trace
    self.sphere = render.SphereShape(6, 6, 10)
    self.cylinder = render.CylinderShape(6)
    self.n_vertex = len(self.trace.display_atoms)*self.sphere.n_vertex
    self.n_vertex += len(self.trace.bonds)*self.cylinder.n_vertex

  def render_to_buffer(self, vertex_buffer):
    grey = [0.7]*3
    color_by_ss = {
      'C': (0.5, 0.5, 0.5),
      'H': (1.0, 0.4, 0.4),
      'E': (0.4, 0.4, 1.0)
    }
    for atom in self.trace.display_atoms:
      point = atom.pos
      if hasattr(atom, 'res_objid'):
        objid = atom.res_objid
        color = color_by_ss[atom.residue.ss]
      else:
        objid = 0
        color = grey
      self.sphere.render_to_center(
          vertex_buffer,
          point,
          point,
          point,
          0.02,
          color,
          objid)
    for i, bond in enumerate(self.trace.bonds):
      if hasattr(bond.atom1, 'res_objid'):
        color = color_by_ss[bond.atom1.residue.ss]
        objid = bond.atom1.res_objid
      else:
        color = grey
        objid = 0
      self.cylinder.render_to_center(
          vertex_buffer,
          bond.atom1.pos,
          bond.tangent,
          bond.up,
          0.2,
          color,
          objid)



def make_ball_and_stick_mesh(trace):
  renderers = [
    BallAndStickRenderer(trace),
  ]
  n_vertex = sum(p.n_vertex for p in renderers)
  vertex_buffer = render.IndexedVertexBuffer(n_vertex)
  for r in renderers:
    r.render_to_buffer(vertex_buffer)
  return vertex_buffer


class PyBall:
  """
  Ties everything together.
  """

  def __init__(self, title, pdb):
    self.width = 640
    self.height = 480

    self.is_mouse_left_down = False
    self.is_mouse_right_down  = False

    self.save_mouse_x = 0.0
    self.save_mouse_y = 0.0

    self.init_glut(title)

    # now glut is initialized, build shaders
    self.shader_catalog = shader.ShaderCatalog()
    self.shader = self.shader_catalog.shader
    
    self.soup = pdbatoms.Soup(pdb)
    self.rendered_soup = RenderedSoup(self.soup)

    self.camera = camera.Camera()
    self.camera.rescale(self.rendered_soup.scale)
    self.camera.set_center(self.rendered_soup.center)

    self.new_camera = camera.Camera()
    self.new_camera.rescale(self.rendered_soup.scale)
    self.n_step_animate = 0

    self.cylinder_draw_object = make_cylinder_trace_mesh(self.rendered_soup, 6, 5, 10)
    self.ribbon_draw_object = make_carton_mesh(self.rendered_soup)
    self.ball_stick_draw_object = make_ball_and_stick_mesh(self.rendered_soup)

    self.is_stick = False
    self.draw_objects = [
        self.ribbon_draw_object,
    ]
    self.set_callbacks()

    self.last = time.time()

  def init_glut(self, title):
    glutInit()
    glutInitWindowSize(self.width, self.height)
    glutInitDisplayMode(GLUT_RGBA|GLUT_DOUBLE|GLUT_DEPTH)
    glutCreateWindow(title)
    
  def draw_to_gl(self):
    glUseProgram(self.shader.program)
    self.shader.bind_camera(self.camera)
    for draw_object in self.draw_objects:
      draw_object.draw(self.shader)

  def display(self):
    """window redisplay callback."""
    glClear(GL_COLOR_BUFFER_BIT|GL_DEPTH_BUFFER_BIT)
    glClearColor(0.0, 0.0, 0.0, 0.0)
    glDisable(GL_CULL_FACE)
    glEnable(GL_DEPTH_TEST)
    glDepthFunc(GL_LEQUAL)
    self.shader = self.shader_catalog.catalog['default']
    self.draw_to_gl()
    glDisable(GL_DEPTH_TEST)
    glColor3f(1, 1, 1)
    glRasterPos2f(100, 100)
    for ch in 'hello':
      glutBitmapCharacter(GLUT_BITMAP_9_BY_15, c_int(ord(ch)))
    glutSwapBuffers()

  def pick(self, x, y):
    glDisable(GL_BLEND)
    glClearColor(0.0, 0.0, 0.0, 0.0)
    glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
    glCullFace(GL_FRONT)
    glEnable(GL_CULL_FACE)
    self.shader = self.shader_catalog.catalog['select']
    self.draw_to_gl()
    pixels = (c_float*4)()
    glReadPixels(x, self.height - y, 1, 1, GL_RGBA, GL_FLOAT, pixels)
    self.objid = int(pixels[2]*255*256*256)
    self.objid += int(pixels[1]*255*256)
    self.objid += int(pixels[0]*255)

  def reshape(self, width, height):
    """window reshape callback."""
    glViewport(0, 0, width, height)
    radius = .5 * min(width, height)
    self.camera.set_screen(width/radius, height/radius)
    self.display() 

  def get_window_dims(self):
    self.width = glutGet(GLUT_WINDOW_WIDTH)
    self.height = glutGet(GLUT_WINDOW_HEIGHT)

  def keyboard(self, c, x=0, y=0):
    """keyboard callback."""
    if c == b'p':
      is_perspective = not self.camera.is_perspective
      self.camera.is_perspective = is_perspective
      self.reshape(glutGet(GLUT_WINDOW_WIDTH), glutGet(GLUT_WINDOW_HEIGHT))
    elif c == b'l':
      is_lighting = not self.camera.is_lighting
      self.camera.is_lighting = is_lighting
    elif c == b'q':
      sys.exit(0)
    elif c == b's':
      self.is_stick = not self.is_stick
      self.draw_objects = []
      self.draw_objects.append(self.ribbon_draw_object)
      if self.is_stick:
        self.draw_objects.append(self.ball_stick_draw_object)
    glutPostRedisplay()

  def screen2space(self, x, y):
    self.get_window_dims()
    radius = min(self.width, self.height)*self.camera.scale
    return (2.*x-self.width)/radius, -(2.*y-self.height)/radius

  def mouse(self, button, state, x, y):
    if button == GLUT_LEFT_BUTTON:
      self.pick(x, y)
      if state == GLUT_DOWN:
        self.save_objid = self.objid
      else:
        if self.save_objid == self.objid and self.objid > 0:
          atom = self.rendered_soup.objid_ref[self.objid]
          s = str(self.objid) + ' ' + atom_name(atom)
          self.new_camera.center = atom.pos
          self.n_step_animate = 10
          print x, y, s, self.n_step_animate
      self.is_mouse_left_down = (state == GLUT_DOWN)
      self.save_mouse_x, self.save_mouse_y = x, y
    elif button == GLUT_RIGHT_BUTTON:
      self.is_mouse_right_down = (state == GLUT_DOWN)
      self.save_mouse_x, self.save_mouse_y = x, y

  def motion(self, x1, y1):
    if self.is_mouse_left_down:
      old_x, old_y = self.screen2space(self.save_mouse_x, self.save_mouse_y)
      x, y = self.screen2space(x1, y1)
      self.camera.rotate_xy(0.1*(x-old_x), 0.1*(y-old_y))
    if self.is_mouse_right_down:
      diff = (x1-self.save_mouse_x)-(y1-self.save_mouse_y)
      new_scale = exp((diff)*.01)
      self.camera.rescale(new_scale)
    self.save_mouse_x, self.save_mouse_y = x1, y1
    glutPostRedisplay()

  def idle(self):
    now = time.time()
    elapsed = now - self.last
    time_step = 0.02
    if self.n_step_animate > 0:
      n_step = min(int(elapsed/time_step), self.n_step_animate)
      if n_step > 0:
        diff_center = self.new_camera.center - self.camera.center
        fraction = n_step/float(self.n_step_animate)
        self.n_step_animate -= n_step
        self.camera.center += fraction*diff_center
        self.display()
        self.last = now
    else:
      self.last = now

  def set_callbacks(self):
    glutIdleFunc(self.idle)
    glutReshapeFunc(self.reshape)
    glutDisplayFunc(self.display)
    glutKeyboardFunc(self.keyboard)
    glutMouseFunc(self.mouse)
    glutMotionFunc(self.motion)

  def run(self):
    return glutMainLoop()


if __name__ == "__main__":
  pdb = '1ssx.pdb'
  if len(sys.argv) > 1:
    pdb = sys.argv[1]
  title = sys.argv[0].encode()
  pyball = PyBall(title, pdb)
  pyball.run()



