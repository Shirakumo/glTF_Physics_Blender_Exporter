import bpy
from ...io.com.gltf2_io_collision_shapes import *
from ...io.com.gltf2_io_rigid_bodies import *
from io_scene_gltf2.io.com.gltf2_io import Node
from mathutils import Matrix, Euler

# Constant used to construct some quaternions when switching up axis
halfSqrt2 = 2**0.5 * 0.5


class glTF2ExportUserExtension:
    def __init__(self):
        # We need to wait until we create the gltf2UserExtension to import the gltf2 modules
        # Otherwise, it may fail because the gltf2 may not be loaded yet
        from io_scene_gltf2.io.com.gltf2_io_extensions import Extension
        from io_scene_gltf2.io.com.gltf2_io_extensions import ChildOfRootExtension

        self.Extension = Extension
        self.ChildOfRootExtension = ChildOfRootExtension
        self.properties = bpy.context.scene.khr_physics_exporter_props
        self.rbExt = RigidBodiesGlTFExtension()
        self.csExt = CollisionShapesGlTFExtension()

        # Supporting data allowing us to save joints correctly
        self.blenderJointObjects = []
        self.blenderNodeToGltfNode = {}

    def gather_gltf_extensions_hook(self, gltf2_plan, export_settings):
        if not self.properties.enabled:
            return

        if gltf2_plan.extensions is None:
            gltf2_plan.extensions = {}

        if self.rbExt.should_export():
            physicsRootExtension = self.Extension(
                name=rigidBody_Extension_Name,
                extension=self.rbExt.to_dict(),
                required=False,
            )
            gltf2_plan.extensions[rigidBody_Extension_Name] = physicsRootExtension

        if (
            not collisionGeom_Extension_Name in gltf2_plan.extensions
            and self.csExt.should_export()
        ):
            cgRootExtension = self.Extension(
                name=collisionGeom_Extension_Name,
                extension=self.csExt.to_dict(),
                required=False,
            )
            gltf2_plan.extensions[collisionGeom_Extension_Name] = cgRootExtension

    def gather_scene_hook(self, gltf2_scene, blender_scene, export_settings):
        if not self.properties.enabled:
            return

        #
        # Export any joints we've seen. These joints may need additional gltf nodes
        # created, in order to supply the pivot transform
        #
        for joint_node in self.blenderJointObjects:
            gltf2_object = self.blenderNodeToGltfNode[joint_node]
            jointData = self._generateJointData(
                joint_node, gltf2_object, export_settings
            )
            # Blender allows a joint to be specified at any point in the scene
            # tree, and the joint points to bodyA/bodyB while the glTF_Physics
            # spec expects that the joint is attached to a child node of bodyA
            # (determining the joint space in bodyA) and points to a child node
            # of bodyB (defining the joint in bodyB space). Make new nodes to
            # contain those transforms.

            bodyA = joint_node.rigid_body_constraint.object1
            aFromWorld = bodyA.matrix_world.copy() if bodyA else Matrix()
            aFromWorld.invert()
            bodyB = joint_node.rigid_body_constraint.object2
            bFromWorld = bodyB.matrix_world.copy() if bodyB else Matrix()
            bFromWorld.invert()

            worldFromJoint = joint_node.matrix_world.copy()
            jointFromBodyA = aFromWorld @ worldFromJoint
            jointFromBodyB = bFromWorld @ worldFromJoint

            # gltf_A/B are the nodes connected to the constraint
            # jointInA/B are the pivots in the space of their connected node
            gltf_B = self.blenderNodeToGltfNode[bodyB]

            jointInB = self._constructNode(
                "jointSpaceB",
                jointFromBodyB.to_translation(),
                jointFromBodyB.to_quaternion(),
                export_settings,
            )
            gltf_B.children.append(jointInB)
            jointData.connected_node = jointInB

            gltf_A = self.blenderNodeToGltfNode[bodyA]
            jointInA = self._constructNode(
                "jointSpaceA",
                jointFromBodyA.to_translation(),
                jointFromBodyA.to_quaternion(),
                export_settings,
            )
            # <todo.eoin Don't stomp exising extension:
            jointInA.extensions[rigidBody_Extension_Name] = self.Extension(
                name=rigidBody_Extension_Name,
                extension={"joint": jointData.to_dict()},
                required=False,
            )
            gltf_A.children.append(jointInA)

    def gather_node_hook(self, gltf2_object, blender_object, export_settings):
        try:
            self.gather_node_hook2(gltf2_object, blender_object, export_settings)
        except:
            import traceback

            print(traceback.format_exc())

    def gather_node_hook2(self, gltf2_object, blender_object, export_settings):
        if self.properties.enabled:
            self.blenderNodeToGltfNode[blender_object] = gltf2_object

            if gltf2_object.extensions is None:
                # <todo.eoin Pretty sure this is never hit, due to export_user_extensions()
                gltf2_object.extensions = {}

            extension_data = RigidBodiesNodeExtension()
            # Blender has no way to specify a shape without a rigid body. Instead, a single shape is
            # specified by being a child of a body whose collider type is "Compound Parent"
            if (
                blender_object.rigid_body
                and blender_object.rigid_body.enabled
                and not self._isPartOfCompound(blender_object)
            ):
                rb = blender_object.rigid_body
                extraProps = blender_object.khr_physics_extra_props

                motion = Motion()

                if rb.kinematic:
                    motion.is_kinematic = rb.kinematic

                motion.mass = rb.mass
                if extraProps.infinite_mass:
                    motion.mass = 0

                if extraProps.gravity_factor != 1.0:
                    motion.gravity_factor = extraProps.gravity_factor

                lv = self.__convert_swizzle_location(
                    Vector(extraProps.linear_velocity), export_settings
                )
                if lv.length_squared != 0:
                    motion.linear_velocity = lv
                av = self.__convert_swizzle_location(
                    Vector(extraProps.angular_velocity), export_settings
                )
                if av.length_squared != 0:
                    motion.angular_velocity = av

                if extraProps.enable_com_override:
                    motion.center_of_mass = self.__convert_swizzle_location(
                        Vector(extraProps.center_of_mass), export_settings
                    )

                if extraProps.enable_inertia_override:
                    motion.inertia_diagonal = self.__convert_swizzle_scale(
                        extraProps.inertia_major_axis, export_settings
                    )
                    motion.inertia_orientation = Euler(
                        blender_object.khr_physics_extra_props.inertia_orientation
                    ).to_quaternion()

                extension_data.motion = motion

            if blender_object.rigid_body:
                shape_data = self._generateShapeData(
                    blender_object, gltf2_object, export_settings
                )
                if shape_data:
                    shape_obj = self.ChildOfRootExtension(
                        name=collisionGeom_Extension_Name,
                        path=["shapes"],
                        required=False,
                        extension=shape_data.to_dict(),
                    )
                    filter_obj = self._generateFilterRootObject(blender_object)
                    extraProps = blender_object.khr_physics_extra_props
                    if extraProps.is_trigger:
                        extension_data.trigger = Trigger()
                        extension_data.trigger.shape = shape_obj
                        extension_data.trigger.collision_filter = filter_obj
                    else:
                        extension_data.collider = Collider()
                        extension_data.collider.shape = shape_obj
                        extension_data.collider.collision_filter = filter_obj
                        extension_data.collider.physics_material = (
                            self._generateMaterialRootObject(blender_object)
                        )

            if blender_object.rigid_body_constraint:
                # Because joints refer to another node in the scene, which may not be processed yet,
                # We'll just save all the joint objects we see and process them later.
                self.blenderJointObjects.append(blender_object)

            if (
                blender_object.rigid_body != None
                or blender_object.rigid_body_constraint != None
            ):
                gltf2_object.extensions[rigidBody_Extension_Name] = self.Extension(
                    name=rigidBody_Extension_Name,
                    extension=extension_data.to_dict(),
                    required=False,
                )

    def _isPartOfCompound(self, node):
        cur = node.parent
        while cur:
            if cur.rigid_body != None:
                if cur.rigid_body.collision_shape == "COMPOUND":
                    return True
            cur = cur.parent
        return False

    def _generateMaterialRootObject(self, blender_object):
        mat = Material()
        mat.static_friction = blender_object.rigid_body.friction
        mat.dynamic_friction = blender_object.rigid_body.friction
        mat.restitution = blender_object.rigid_body.restitution

        extraProps = blender_object.khr_physics_extra_props
        if extraProps.friction_combine != physics_material_combine_types[0][0]:
            mat.friction_combine = extraProps.friction_combine
        if extraProps.restitution_combine != physics_material_combine_types[0][0]:
            mat.restitution_combine = extraProps.restitution_combine

        return self.ChildOfRootExtension(
            name=rigidBody_Extension_Name,
            path=["physicsMaterials"],
            extension=mat.to_dict(),
            required=False,
        )

    def _generateJointData(self, node, glNode, export_settings):
        """Converts the concrete joint data on `node` to a generic 6DOF representation"""
        joint = node.rigid_body_constraint
        jointData = Joint()
        if not joint.disable_collisions:
            jointData.enable_collision = not joint.disable_collisions

        if export_settings["gltf_yup"]:
            X, Y, Z = (0, 2, 1)
        else:
            X, Y, Z = (0, 1, 2)

        limitSet = JointLimitSet()
        if joint.type == "FIXED":
            limitSet.joint_limits.append(JointLimit.Linear([X, Y, Z], 0, 0))
            limitSet.joint_limits.append(JointLimit.Angular([X, Y, Z], 0, 0))
        elif joint.type == "POINT":
            limitSet.joint_limits.append(JointLimit.Linear([X, Y, Z], 0, 0))
        elif joint.type == "HINGE":
            limitSet.joint_limits.append(JointLimit.Linear([X, Y, Z], 0, 0))

            # Blender always specifies hinge about Z
            limitSet.joint_limits.append(JointLimit.Angular([X, Y], 0, 0))

            if joint.use_limit_ang_z:
                angLimit = JointLimit.Angular([Z])
                angLimit.min_limit = joint.limit_ang_z_lower
                angLimit.max_limit = joint.limit_ang_z_upper
                limitSet.joint_limits.append(angLimit)
        elif joint.type == "SLIDER":
            limitSet.joint_limits.append(JointLimit.Angular([X, Y, Z], 0, 0))

            # Blender always specifies slider limit along X
            limitSet.joint_limits.append(JointLimit.Linear([Y, Z], 0, 0))

            if joint.use_limit_lin_x:
                linLimit = JointLimit.Linear([X])
                linLimit.min_limit = joint.limit_lin_x_lower
                linLimit.max_limit = joint.limit_lin_x_upper
                limitSet.joint_limits.append(linLimit)
        elif joint.type == "PISTON":
            # Blender always specifies slider limit along/around X
            limitSet.joint_limits.append(JointLimit.Angular([Y, Z], 0, 0))
            limitSet.joint_limits.append(JointLimit.Linear([Y, Z], 0, 0))

            if joint.use_limit_lin_x:
                linLimit = JointLimit.Linear([X])
                linLimit.min_limit = joint.limit_lin_x_lower
                linLimit.max_limit = joint.limit_lin_x_upper
                limitSet.joint_limits.append(linLimit)
            if joint.use_limit_ang_x:
                angLimit = JointLimit.Angular([X])
                angLimit.min_limit = joint.limit_ang_x_lower
                angLimit.max_limit = joint.limit_ang_x_upper
                limitSet.joint_limits.append(angLimit)
        elif joint.type in ("GENERIC", "GENERIC_SPRING"):
            # Appears that Blender always uses 1D constraints
            if joint.use_limit_lin_x:
                linLimit = JointLimit.Linear([X])
                linLimit.min_limit = joint.limit_lin_x_lower
                linLimit.max_limit = joint.limit_lin_x_upper
                if joint.type == "GENERIC_SPRING" and joint.use_spring_x:
                    linLimit.spring_constant = joint.spring_stiffness_x
                    linLimit.spring_damping = joint.spring_damping_x
                limitSet.joint_limits.append(linLimit)
            if joint.use_limit_lin_y:
                linLimit = JointLimit.Linear([Y])
                if export_settings["gltf_yup"]:
                    linLimit.min_limit = -joint.limit_lin_y_upper
                    linLimit.max_limit = -joint.limit_lin_y_lower
                else:
                    linLimit.min_limit = joint.limit_lin_y_lower
                    linLimit.max_limit = joint.limit_lin_y_upper
                if joint.type == "GENERIC_SPRING" and joint.use_spring_y:
                    linLimit.spring_constant = joint.spring_stiffness_y
                    linLimit.spring_damping = joint.spring_damping_y
                limitSet.joint_limits.append(linLimit)
            if joint.use_limit_lin_z:
                linLimit = JointLimit.Linear([Z])
                linLimit.min_limit = joint.limit_lin_z_lower
                linLimit.max_limit = joint.limit_lin_z_upper
                if joint.type == "GENERIC_SPRING" and joint.use_spring_z:
                    linLimit.spring_constant = joint.spring_stiffness_z
                    linLimit.spring_damping = joint.spring_damping_z
                limitSet.joint_limits.append(linLimit)

            if joint.use_limit_ang_x:
                angLimit = JointLimit.Angular([X])
                angLimit.min_limit = joint.limit_ang_x_lower
                angLimit.max_limit = joint.limit_ang_x_upper
                if joint.type == "GENERIC_SPRING" and joint.use_spring_ang_x:
                    angLimit.spring_constant = joint.spring_stiffness_ang_x
                    angLimit.spring_damping = joint.spring_damping_ang_x
                limitSet.joint_limits.append(angLimit)
            if joint.use_limit_ang_y:
                angLimit = JointLimit.Angular([Y])
                if export_settings["gltf_yup"]:
                    angLimit.min_limit = -joint.limit_ang_y_upper
                    angLimit.max_limit = -joint.limit_ang_y_lower
                else:
                    angLimit.min_limit = joint.limit_ang_y_lower
                    angLimit.max_limit = joint.limit_ang_y_upper
                if joint.type == "GENERIC_SPRING" and joint.use_spring_ang_y:
                    angLimit.spring_constant = joint.spring_stiffness_ang_y
                    angLimit.spring_damping = joint.spring_damping_ang_y
                limitSet.joint_limits.append(angLimit)
            if joint.use_limit_ang_z:
                angLimit = JointLimit.Angular([Z])
                angLimit.min_limit = joint.limit_ang_z_lower
                angLimit.max_limit = joint.limit_ang_z_upper
                if joint.type == "GENERIC_SPRING" and joint.use_spring_ang_z:
                    angLimit.spring_constant = joint.spring_stiffness_ang_z
                    angLimit.spring_damping = joint.spring_damping_ang_z
                limitSet.joint_limits.append(angLimit)

        jointData.joint_limits = self.ChildOfRootExtension(
            name=rigidBody_Extension_Name,
            path=["physicsJointLimits"],
            extension=limitSet.to_dict(),
            required=False,
        )
        return jointData

    def _generateFilterRootObject(self, node):
        # Blender's native collision filtering has less functionality than the spec enables:
        #    * Children of COMPOUND_PARENT don't have a UI to configure filtering
        #    * An objects' "membership" is always equal to it's "collides with"
        #    * Seems there's no "user friendly" names
        collision_systems = [
            "System_%i" % i
            for (i, enabled) in enumerate(node.rigid_body.collision_collections)
            if enabled
        ]
        collision_filter = CollisionFilter()
        collision_filter.collision_systems = collision_systems
        collision_filter.collide_with_systems = collision_systems
        return self.ChildOfRootExtension(
            name=rigidBody_Extension_Name,
            path=["collisionFilters"],
            required=False,
            extension=collision_filter.to_dict(),
        )

    def _generateShapeData(self, node, glNode, export_settings):
        if node.rigid_body == None or node.rigid_body.collision_shape == "COMPOUND":
            return None
        shape = Shape()

        if node.rigid_body.collision_shape == "CONVEX_HULL":
            shape.type = "convex"
            shape.convex = Convex(glNode.mesh)
            return shape
        elif node.rigid_body.collision_shape == "MESH":
            shape.type = "trimesh"
            shape.trimesh = TriMesh(glNode.mesh)
            return shape
        # If the shape is a geometric primitive, we may have to apply modifiers
        # to see the final geometry. (glNode has already had modifiers applied)
        with self._accessMeshData(node, export_settings) as meshData:
            if node.rigid_body.collision_shape == "SPHERE":
                maxRR = 0
                for v in meshData.vertices:
                    maxRR = max(maxRR, v.co.length_squared)
                shape.type = "sphere"
                shape.sphere = Sphere(radius=maxRR**0.5)
            elif node.rigid_body.collision_shape == "BOX":
                maxHalfExtent = [0, 0, 0]
                for v in meshData.vertices:
                    maxHalfExtent = [
                        max(a, abs(b)) for a, b in zip(maxHalfExtent, v.co)
                    ]
                shape.type = "box"
                shape.box = Box(
                    size=self.__convert_swizzle_scale(maxHalfExtent, export_settings)
                    * 2
                )
            elif node.rigid_body.collision_shape in ("CAPSULE", "CONE", "CYLINDER"):
                if not node.khr_physics_extra_props.cone_capsule_override:
                    # User hasn't overridden shape params, so we need to calculate them
                    # Maybe there's a way to extract them from Blender?
                    primaryAxis = Vector(
                        (0, 0, 1)
                    )  # Use blender's up axis, instead of glTF (and transform later)
                    maxHalfHeight = 0
                    maxRadiusSquared = 0
                    for v in meshData.vertices:
                        maxHalfHeight = max(maxHalfHeight, abs(v.co.dot(primaryAxis)))
                        radiusSquared = (
                            v.co - primaryAxis * v.co.dot(primaryAxis)
                        ).length_squared
                        maxRadiusSquared = max(maxRadiusSquared, radiusSquared)
                    height = maxHalfHeight * 2
                    radiusBottom = maxRadiusSquared**0.5
                    radiusTop = (
                        radiusBottom if node.rigid_body.collision_shape != "CONE" else 0
                    )
                    if node.rigid_body.collision_shape == "CAPSULE":
                        height = height - radiusBottom * 2
                else:
                    height = node.khr_physics_extra_props.cone_capsule_height
                    radiusBottom = (
                        node.khr_physics_extra_props.cone_capsule_radius_bottom
                    )
                    radiusTop = node.khr_physics_extra_props.cone_capsule_radius_top

                if node.rigid_body.collision_shape == "CAPSULE":
                    shape.type = "capsule"
                    shape.capsule = Capsule(
                        height=height,
                        radiusTop=radiusTop,
                        radiusBottom=radiusBottom,
                    )
                else:
                    shape.type = "cylinder"
                    shape.cylinder = Cylinder(
                        height=height,
                        radiusTop=radiusTop,
                        radiusBottom=radiusBottom,
                    )

                if not export_settings["gltf_yup"]:
                    # Add an additional node to align the object, so the shape is oriented correctly when constructed along +Y
                    shape_alignment = self._constructNode(
                        "physicsAlignmentNode",
                        Vector((0, 0, 0)),
                        Quaternion((halfSqrt2, 0, 0, halfSqrt2)),
                        export_settings,
                    )

                    node_ext = RigidBodiesNodeExtension()
                    shape_obj = self.ChildOfRootExtension(
                        name=collisionGeom_Extension_Name,
                        path=["shapes"],
                        required=False,
                        extension=shape.to_dict(),
                    )
                    if node.khr_physics_extra_props.is_trigger:
                        node_ext.trigger = Trigger()
                        node_ext.trigger.collision_filter = (
                            self._generateFilterRootObject(node)
                        )
                        node_ext.trigger.shape = shape_obj
                    else:
                        node_ext.collider = Collider()
                        node_ext.collider.physics_material = (
                            self._generateMaterialRootObject(node)
                        )
                        node_ext.collider.collision_filter = (
                            self._generateFilterRootObject(node)
                        )
                        node_ext.collider.shape = shape_obj

                    shape_alignment.extensions[
                        rigidBody_Extension_Name
                    ] = self.Extension(
                        name=rigidBody_Extension_Name,
                        extension=node_ext.to_dict(),
                        required=False,
                    )
                    glNode.children.append(shape_alignment)

                    # We've added the shape data to a child of glNode;
                    # return None so that the glNode doesn't get shape data,
                    return None
        return shape

    def _accessMeshData(self, node, export_settings):
        """RAII-style function to access mesh data with modifiers attached"""

        class ScopedMesh:
            def __init__(self, node, export_settings):
                self.node = node
                self.export_settings = export_settings
                self.modifiedNode = None

            def __enter__(self):
                if self.export_settings["gltf_apply"]:
                    depsGraph = bpy.context.evaluated_depsgraph_get()
                    self.modifiedNode = node.evaluated_get(depsGraph)
                    return self.modifiedNode.to_mesh(
                        preserve_all_data_layers=True, depsgraph=depsGraph
                    )
                else:
                    return self.node.data

            def __exit__(self, *args):
                if self.modifiedNode:
                    self.modifiedNode.to_mesh_clear()

        return ScopedMesh(node, export_settings)

    def _constructNode(self, name, translation, rotation, export_settings):
        return Node(
            name=name,
            translation=[
                x for x in self.__convert_swizzle_location(translation, export_settings)
            ],
            rotation=self._serializeQuaternion(
                self.__convert_swizzle_rotation(rotation, export_settings)
            ),
            matrix=[],
            camera=None,
            children=[],
            extensions={},
            extras=None,
            mesh=None,
            scale=None,
            skin=None,
            weights=None,
        )

    # Copy-pasted from the glTF exporter; are they accessible some other way, without having to duplicate?
    def __convert_swizzle_location(self, loc, export_settings):
        """Convert a location from Blender coordinate system to glTF coordinate system."""
        if export_settings["gltf_yup"]:
            return Vector((loc[0], loc[2], -loc[1]))
        else:
            return Vector((loc[0], loc[1], loc[2]))

    # Copy-pasted from the glTF exporter; are they accessible some other way, without having to duplicate?
    def __convert_swizzle_scale(self, scale, export_settings):
        """Convert a scale from Blender coordinate system to glTF coordinate system."""
        if export_settings["gltf_yup"]:
            return Vector((scale[0], scale[2], scale[1]))
        else:
            return Vector((scale[0], scale[1], scale[2]))

    # Copy-pasted from the glTF exporter; are they accessible some other way, without having to duplicate?
    def __convert_swizzle_rotation(self, rot, export_settings):
        """
        Convert a quaternion rotation from Blender coordinate system to glTF coordinate system.
        'w' is still at first position.
        """
        if export_settings["gltf_yup"]:
            return Quaternion((rot[0], rot[1], rot[3], -rot[2]))
        else:
            return Quaternion((rot[0], rot[1], rot[2], rot[3]))

    def _serializeQuaternion(self, q):
        """Converts a quaternion to a type which can be serialized, with components in correct order"""
        return [q.x, q.y, q.z, q.w]
