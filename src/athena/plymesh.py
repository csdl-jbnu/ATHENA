from pathlib import Path
import struct
import itertools

from PySide2.QtCore import QByteArray, Qt
from PySide2.Qt3DCore import Qt3DCore
from PySide2.Qt3DRender import Qt3DRender

from plyfile import PlyData, PlyElement
import numpy as np

from athena import geom
from earcut import earcut

# The base types enumeration
_basetypes = Qt3DRender.QAttribute.VertexBaseType

# Map from the enumeration to (byte_width, struct_code) pairs
# This dict is unzipped into two convenience dicts below.
_basetype_data = { _basetypes.Byte : (1,'b'), _basetypes.UnsignedByte : (1,'B'),
                     _basetypes.Short: (2, 'h'), _basetypes.UnsignedShort : (2,'H'),
                     _basetypes.Int  : (4, 'i'), _basetypes.UnsignedInt : (4,'I'),
                     _basetypes.HalfFloat : (2, 'e'),
                     _basetypes.Float : (4, 'f'),
                     _basetypes.Double : (8, 'd') }

# Map of Qt3D base types to byte widths
_basetype_widths = { k: v[0] for k,v in _basetype_data.items()}

# Map of Qt3D base types to codes for struct.unpack
_basetype_struct_codes = { k: v[1] for k,v in _basetype_data.items()}

# Map of Qt3D base types to numpy types
_basetype_numpy_codes = { k: np.sctypeDict[v] for k,v in _basetype_struct_codes.items()}

def tri_norm(a,b,c):
    tri_normal = np.cross( a-b, a-c)
    tri_normal /= np.linalg.norm(tri_normal)
    return tri_normal

class EdgeDict:
    def __init__(self):
        self.edges = dict()

    @classmethod
    def _key(a,b):
        return (min(a,b), max(a,b))

    def addEdge( self, a, b, idx ):
        self.edges[ _key(a,b) ] = idx

    def hasEdge( self, a, b ):
        return _key(a,b) in self.edges

    def getEdge( self, a, b ):
        return self.edges[_key(a,b)]

def edge( a, b ):
    return (min(a,b), max(a,b))

def edgeIter(poly):
    it = iter(poly)
    i0 = next(it)
    i_last = i0
    for i in it:
        yield edge( i_last, i )
        i_last = i
    yield edge( i_last, i0 )
   

class PlyMesh(Qt3DCore.QEntity):
    def __init__(self, parent, plydata):
        super(PlyMesh, self).__init__(parent)

        vertices = plydata['vertex'].data
        faces = plydata['face'].data['vertex_indices']

        # Count the faces in the model that have more than 3 vertices: we will need to
        # triangulate these
        large_faces = [x for x in faces if len(x) > 3]
        num_large_faces = len(large_faces)
        num_tris = len(faces) - num_large_faces
        num_interior_tris = sum(len(x) for x in large_faces)

        #total_vertices = len(vertices) + num_large_faces
        total_vertices = len(vertices) * 2
        total_tris = num_tris + num_interior_tris

        vertex_basetype = _basetypes.Float
        if( total_tris < 30000 ):
            index_basetype = _basetypes.UnsignedShort
        else:
            index_basetype = _basetypes.UnsignedInt

        vertex_nparr = np.zeros([total_vertices,7],dtype=_basetype_numpy_codes[vertex_basetype])
        # Fill with the input vertices
        vertex_nparr[:len(vertices),0] = vertices['x']
        vertex_nparr[:len(vertices),1] = vertices['y']
        vertex_nparr[:len(vertices),2] = vertices['z']
        vtx_idx = len(vertices)

        tri_nparr = np.zeros([total_tris,3],dtype=_basetype_numpy_codes[index_basetype])
        tri_idx = 0


        def add_vtx(v,interior=1):
            nonlocal vtx_idx
            vertex_nparr[vtx_idx,:]=v
            vertex_nparr[vtx_idx,6] = interior
            #print("New vtx:", vertex_nparr[vtx_idx])
            vtx_idx += 1
            return vtx_idx - 1

        internal_vertices = {}
        def internalize_vtx( v ):
            nonlocal internal_vertices
            if v not in internal_vertices:
                vert_data = vertex_nparr[v,:]
                new_idx = add_vtx(vert_data)
                internal_vertices[v] = new_idx
            return internal_vertices[v]

        def add_tri(t):
            nonlocal tri_idx
            tri_nparr[tri_idx,:]=t
            tri_idx += 1
            return tri_idx - 1

        # create the index buffer and any needed internal vertices
        for poly in faces:
            if( len(poly) == 3 ):
                add_tri(poly)
            else:
                external_edges = set( edgeIter( poly ) )
                assert( len(external_edges) == len(poly) )
                poly_verts = np.take(vertex_nparr, poly, axis=0)
                # The xyz geometry of this polygon
                poly_geom = np.c_[ poly_verts[:,0:3] ]
                # Compute the normal of this polygon from the first three verts;
                # we'll need this later to determine winding direction for the
                # triangulated faces
                poly_normal = tri_norm( *(poly_geom[x,:] for x in range(3)) )
                # Geometric centroid of the polygon
                G = np.average( poly_geom, axis=0 )
                offset_geom = poly_geom - G
                # Singular value decomposition: we want to map the 3D coordinates
                # to a 2D subspace that can be fed into a 2D triangulation algorithm.
                # For this we only need the last return value.
                _, _, vh = np.linalg.svd(offset_geom)
                vt = vh[:2,:].T
                xy_coords = np.dot(offset_geom, vt)
                flattened = earcut.flatten([xy_coords,[]])
                new_tris = earcut.earcut(flattened['vertices'],None,flattened['dimensions'])

                # Now we have the new triangles from earcut.
                # Check the first one's normal; if it doesn't match the polygon normal,
                # then we'll assume the 2D projection reversed our triangle windings.
                geom_tri0 = poly_geom.take(new_tris[0:3], axis=0)
                tri0_norm = tri_norm(*(geom_tri0[x,:] for x in range(3)))
                normcheck = np.dot(tri0_norm, poly_normal)
                flip = False
                if( not np.isclose(normcheck, 1.0, rtol=1e-1) ):
                    flip = True

                # Now add new triangles to the index buffer
                for a,b,c in geom.grouper(new_tris,3):
                    idx_a = poly[a]
                    idx_b = poly[b]
                    idx_c = poly[c]
                    if( flip ): 
                        idx_b, idx_c = idx_c, idx_b
                    edge_a = edge( idx_a, idx_b )
                    edge_b = edge( idx_b, idx_c )
                    edge_c = edge( idx_c, idx_a )
                    if( edge_a not in external_edges ):
                        idx_a = internalize_vtx( idx_a )
                    if( edge_b not in external_edges ):
                        idx_b = internalize_vtx( idx_b )
                    if( edge_c not in external_edges ):
                        idx_c = internalize_vtx( idx_c )
                    add_tri(np.array([idx_a,idx_b,idx_c]))

        # Sanity checks
        #assert( vtx_idx == total_vertices )
        #assert( tri_idx == total_tris )

        self.geometry = Qt3DRender.QGeometry(self)

        # Create qt3d vertex buffers
        rawstring = vertex_nparr.tobytes()
        self.qvbytes = QByteArray(rawstring)
        self.qvbuf = Qt3DRender.QBuffer(parent)
        self.qvbuf.setData(self.qvbytes)

        # Position attribute
        self.positionAttr = Qt3DRender.QAttribute(parent)
        self.positionAttr.setName( Qt3DRender.QAttribute.defaultPositionAttributeName() )
        self.positionAttr.setVertexBaseType(vertex_basetype)
        self.positionAttr.setVertexSize(3)
        self.positionAttr.setAttributeType(Qt3DRender.QAttribute.VertexAttribute)
        self.positionAttr.setBuffer(self.qvbuf)
        self.positionAttr.setByteStride(7*_basetype_widths[vertex_basetype])
        self.positionAttr.setCount(len(vertex_nparr))
        self.geometry.addAttribute(self.positionAttr)

        # Interior attribute
        self.interiorAttr = Qt3DRender.QAttribute(parent)
        self.interiorAttr.setName( 'vertexInterior' )
        self.interiorAttr.setVertexBaseType(vertex_basetype)
        self.interiorAttr.setVertexSize(1)
        self.interiorAttr.setAttributeType(Qt3DRender.QAttribute.VertexAttribute)
        self.interiorAttr.setBuffer(self.qvbuf)
        self.interiorAttr.setByteStride(7*_basetype_widths[vertex_basetype])
        self.interiorAttr.setByteOffset(6*_basetype_widths[vertex_basetype])
        self.interiorAttr.setCount(len(vertex_nparr))
        self.geometry.addAttribute(self.interiorAttr)

        rawstring = tri_nparr.tobytes()
        
        self.qibytes = QByteArray(rawstring)
        self.qibuf = Qt3DRender.QBuffer(parent)
        self.qibuf.setData(self.qibytes)

        self.indexAttr = Qt3DRender.QAttribute(self.geometry)
        self.indexAttr.setVertexBaseType(index_basetype)
        self.indexAttr.setAttributeType(Qt3DRender.QAttribute.IndexAttribute)
        self.indexAttr.setBuffer(self.qibuf)
        self.indexAttr.setCount(3*total_tris)
        self.geometry.addAttribute(self.indexAttr)

        self.lineMesh = Qt3DRender.QGeometryRenderer(parent)
        self.lineMesh.setGeometry(self.geometry)
        self.lineMesh.setPrimitiveType( Qt3DRender.QGeometryRenderer.Triangles )

        self.addComponent(self.lineMesh)




class WireOutline(Qt3DCore.QEntity):
    # This is a lines-based outline renderer, not currently used
    def __init__(self, parent, geom, plydata):
        super(WireOutline, self).__init__(parent)

        vertices = plydata['vertex'].data
        faces = plydata['face'].data

        self.geometry = Qt3DRender.QGeometry(self)

        # borrow the position attribute buffer from geom
        vatt = getQAttribute( geom, att_name = Qt3DRender.QAttribute.defaultPositionAttributeName() )
        self.geometry.addAttribute(vatt)

        # Now create the index attribute.  This is different from the Qt3D mesh, which has been triangulated,
        # so we'll need to iterate over the .ply faces and build up our own buffer.

        def edge_index_iter():
            '''Iterate all pairs of connected vertices (i.e. all edges) in the faces structure

            May repeat edges.  Returns (x,y) pairs with x always less than y.
            '''
            for poly in faces:
                indices = poly[0]
                it = iter(indices)
                i0 = next(it)
                i_last = i0
                for i in it:
                    pair = (min(i_last, i), max(i_last, i))
                    i_last = i
                    yield pair
                yield (min(i_last,i0), max(i_last,i0))

        unique_edges = set(pair for pair in edge_index_iter())

        #  use the same basetype as the Qt3D mesh, since presumably that file loader chose a suitable type
        iatt = getQAttribute(geom, att_type=Qt3DRender.QAttribute.IndexAttribute)
        index_type = iatt.vertexBaseType()
        index_buffer_np = np.array(list(unique_edges), dtype=_basetype_numpy_codes[index_type])
        rawstring = index_buffer_np.tobytes()
        
        self.qibytes = QByteArray(rawstring)
        self.qibuf = Qt3DRender.QBuffer(self.geometry)
        self.qibuf.setData(self.qibytes)

        self.indexAttr = Qt3DRender.QAttribute(self.geometry)
        self.indexAttr.setVertexBaseType(index_type)
        self.indexAttr.setAttributeType(Qt3DRender.QAttribute.IndexAttribute)
        self.indexAttr.setBuffer(self.qibuf)
        self.indexAttr.setCount(index_buffer_np.size)
        self.geometry.addAttribute(self.indexAttr)

        self.lineMesh = Qt3DRender.QGeometryRenderer(parent)
        self.lineMesh.setGeometry(self.geometry)
        self.lineMesh.setPrimitiveType( Qt3DRender.QGeometryRenderer.Lines )

        #self.lineMaterial = Qt3DExtras.QPhongMaterial(parent)
        #self.lineMaterial.setAmbient(QColor(255,255,0))

        #self.lineEntity = Qt3DCore.QEntity(parent)
        self.addComponent(self.lineMesh)
        #self.lineEntity.addComponent(self.lineMaterial)