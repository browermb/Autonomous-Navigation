#!/usr/bin/env python

import math
import gps_util
import geometry_util
import rospy
from navigation_msgs.msg import WaypointsArray, VelAngle
from nav_msgs.msg import Path, Odometry
from std_msgs.msg import Header, Float32
from geometry_msgs.msg import PoseStamped, Point
from visualization_msgs.msg import Marker
import tf.transformations as tf

import cubic_spline_planner
import pure_pursuit

'''
This class contains the code to track and fit a path
for the cart to follow
'''
class Mind(object):
    def __init__(self):
        rospy.init_node('Mind')

        self.odom = Odometry()
        self.google_points = []
        self.rp_dist = 99999999999
        self.stop_thresh = 5 #this is how many seconds an object is away

        #waypoints are points coming in from the map
        self.waypoints_s = rospy.Subscriber('/waypoints', WaypointsArray,
                                            self.waypoints_callback, queue_size=10)
        self.odom_sub = rospy.Subscriber('/pose_and_speed', Odometry,
                                         self.odom_callback, queue_size=10)
        self.rp_distance_sub = rospy.Subscriber('/rp_distance', Float32,
                                                self.rp_callback, queue_size=10)

        #publishes points that are now in gps coordinates
        self.points_pub = rospy.Publisher('/points', Path, queue_size=10, latch=True)
        self.path_pub = rospy.Publisher('/path', Path, queue_size=10, latch=True)
        self.motion_pub = rospy.Publisher('/nav_cmd', VelAngle, queue_size=10)
        self.target_pub = rospy.Publisher('/target_point', Marker, queue_size=10)
        self.target_twist_pub = rospy.Publisher('/target_twist', Marker, queue_size=10)

        rospy.spin()

    def odom_callback(self, msg):
        self.odom = msg

    def rp_callback(self, msg):
        #used to stop the vehicle if objects are within a certain distance of the cart
        if msg.data <= 0.5:
            self.rp_dist = 99999999
        else:
            self.rp_dist = msg.data


    #Converts waypoints into points in gps coordinates as opposed to the map
    def waypoints_callback(self, msg):
        for gps_point in msg.waypoints:
            point = gps_util.get_point(gps_point)
            self.google_points.append(point)

        self.create_path()
    '''
    Creates a path for the cart with a set of google_points
    Adds 15 more points between the google points
    Intermediate points are added for a better fitting spline
    '''
    def create_path(self):

        #creates 15 intermediate points
        google_points_plus = geometry_util.add_intermediate_points(self.google_points, 15.0)

        ax = []
        ay = []

        #Creates another path to add intermediate points for a better fitting spline
        extra_points = Path()
        extra_points.header = Header()
        extra_points.header.frame_id = '/map'

        #Creates a list of the x's and y's to be used when calculating the spline
        for p in google_points_plus:
            extra_points.poses.append(create_pose_stamped(p))
            ax.append(p.x)
            ay.append(p.y)

        self.points_pub.publish(extra_points)

        cx, cy = cubic_spline_planner.calc_spline_course(ax, ay, ds=0.1)


        #Create path object for the cart to follow
        path = Path()
        path.header = Header()
        path.header.frame_id = '/map'

        for i in range(0, len(cx)):
            curve_point = Point()
            curve_point.x = cx[i]
            curve_point.y = cy[i]
            path.poses.append(create_pose_stamped(curve_point))

        self.path_pub.publish(path)

        target_speed = 10.0 / 3.6  # [m/s]

        # initial state
        pose = self.odom.pose.pose
        twist = self.odom.twist.twist

        quat = (pose.orientation.x, pose.orientation.y, pose.orientation.z, pose.orientation.w)
        angles = tf.euler_from_quaternion(quat)
        initial_v = math.sqrt(twist.linear.x ** 2 + twist.linear.y ** 2)
	    #TODO state has to be where we start
        state = State(x=pose.position.x, y=pose.position.y, yaw=angles[2], v=initial_v)

        last_index = len(cx) - 1
        time = 0.0
        x = [state.x]
        y = [state.y]
        yaw = [state.yaw]
        v = [state.v]
        t = [0.0]
        target_ind = pure_pursuit.calc_target_index(state, cx, cy)

        #continue to loop while we have not hit the target
        while last_index > target_ind:
            ai = pure_pursuit.PIDControl(target_speed, state.v)
            di, target_ind = pure_pursuit.pure_pursuit_control(state, cx, cy, target_ind)

            #publish our desired position
            mkr = create_marker(cx[target_ind], cy[target_ind], '/map')
            self.target_pub.publish(mkr)

            #publish an arrow with our twist
            arrow = create_marker(0, 0, '/base_link')
            arrow.type = 0 #arrow
            arrow.scale.x = 2.0
            arrow.scale.y = 1.0
            arrow.scale.z = 1.0
            arrow.color.r = 1.0
            arrow.color.g = 0.0
            arrow.color.b = 0.0

            quater = tf.quaternion_from_euler(0, 0, di)
            arrow.pose.orientation.x = quater[0]
            arrow.pose.orientation.y = quater[1]
            arrow.pose.orientation.z = quater[2]
            arrow.pose.orientation.w = quater[3]
            self.target_twist_pub.publish(arrow)

            state = self.update(state, ai, di)

            x.append(state.x)
            y.append(state.y)
            yaw.append(state.yaw)
            v.append(state.v)
            t.append(time)

        rospy.logerr("Done navigating")
        msg = VelAngle()
        msg.vel = 0
        msg.angle = 0
        msg.vel_curr = 0
        self.motion_pub.publish(msg)


    '''
    Updates the carts position by a given state and delta
    '''
    def update(self, state, a, delta):

        pose = self.odom.pose.pose
        twist = self.odom.twist.twist

        current_spd = math.sqrt(twist.linear.x ** 2 + twist.linear.y ** 2)

        msg = VelAngle()
        msg.vel = a
        msg.angle = (delta*180)/math.pi
        msg.vel_curr = current_spd
        self.motion_pub.publish(msg)

        state.x = pose.position.x
        state.y = pose.position.y

        quat = (pose.orientation.x, pose.orientation.y, pose.orientation.z, pose.orientation.w)
        angles = tf.euler_from_quaternion(quat)

        state.yaw = angles[2]

        state.v = math.sqrt(twist.linear.x ** 2 + twist.linear.y ** 2)

        return state


class State:
    def __init__(self, x=0.0, y=0.0, yaw=0.0, v=0.0):
        self.x = x
        self.y = y
        self.yaw = yaw
        self.v = v

def create_pose_stamped(point):
    stamped = PoseStamped()
    stamped.header = Header()
    stamped.header.frame_id = '/map'
    stamped.pose.position = point
    return stamped

def create_marker(x, y, frame_id):
    marker = Marker()
    marker.header.frame_id = frame_id
    marker.header.stamp = rospy.Time.now()
    marker.ns = "my_namespace"
    marker.id = 0
    marker.type = 1 #cube
    marker.action = 0 #add
    marker.pose.position.x = x
    marker.pose.position.y = y
    marker.pose.position.z = 0

    marker.pose.orientation.x = 0.0
    marker.pose.orientation.y = 0.0
    marker.pose.orientation.z = 0.0
    marker.pose.orientation.w = 1.0
    marker.scale.x = 1.0
    marker.scale.y = 1.0
    marker.scale.z = 1.0
    marker.color.a = 1.0
    marker.color.r = 0.0
    marker.color.g = 1.0
    marker.color.b = 0.0

    return marker

if __name__ == "__main__":
    try:
        Mind()
    except rospy.ROSInterruptException:
        pass
