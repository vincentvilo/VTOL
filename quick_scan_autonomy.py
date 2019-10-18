import json
import math
import time
from conversions import *
from dronekit import connect, Command, VehicleMode, LocationGlobalRelative
from threading import Thread
from pymavlink import mavutil

# first import gives access to global variables in "autonomy" namespace
# second import is for functions
import autonomy
from autonomy import comm_simulation, acknowledge, bad_msg, takeoff, land

search_area = None  # search area object, populated by callback on start

# Represents a search area for quick scan aerial vehicles
class SearchArea:
    def __init__(self, center, rad1, rad2):
        self.center = center  # tuple containing coor. of center point
        self.rad1 = rad1  # first search radius from center
        self.rad2 = rad2  # second search radius from center

    def __str__(self):
        return "SearchArea(" + \
               str(self.center) + ", " + \
               str(self.rad1) + ", " + \
               str(self.rad2) + ")"

# Callback function for messages from GCS, parses JSON message and sets globals
def xbee_callback(message):
    global start_mission
    global pause_mission
    global stop_mission
    global search_area

    address = message.remote_device.get_64bit_addr()
    msg = json.loads(message.data)
    print("Received data from %s: %s" % (address, msg))

    try:
        msg_type = msg["type"]

        if msg_type == "start":
            area = msg["searchArea"]
            search_area = SearchArea(area["center"], area["rad1"], area["rad2"])
            autonomy.start_mission = True
            acknowledge(address, msg_type)

        elif msg_type == "pause":
            autonomy.pause_mission = True
            acknowledge(address, msg_type)

        elif msg_type == "resume":
            autonomy.pause_mission = False
            acknowledge(address, msg_type)

        elif msg_type == "stop":
            autonomy.stop_mission = True
            acknowledge(address, msg_type)

        else:
            bad_msg(address, "Unknown message type: \'" + msg_type + "\'")

    # KeyError if message was missing an expected key
    except KeyError as e:
        bad_msg(address, "Missing \'" + e.args[0] + "\' key")


# Generate waypoints for VTOL
def generateWaypoints(configs, search_area):
    print("Begin generating waypoints")
    
    waypointsNED = []
    waypointsLLA = []
    
    origin = search_area.center
    radius = search_area.rad2
    
    # pre-definied in configs file
    altitude = configs["altitude"]
    lat = origin[0]
    lon = origin[1]
    centerX, centerY, centerZ = geodetic2ecef(lat, lon, altitude)
    n, e, d = ecef2ned(centerX, centerY, centerZ, lat, lon, altitude)
    waypointsLLA.append([lat, lon, altitude])
    waypointsNED.append([n, e, d])
    
    fovH = math.radians(62.2)     # raspicam horizontal FOV
    fovV = math.radians(48.8)     # raspicam vertical FOV
    boxH = 2 * altitude / math.tan(fovH / 2)     # height of bounding box
    boxV = 2 * altitude / math.tan(fovV / 2)     # width of bounding box
    
    b = (0.75 * boxH) / (2 * math.pi)   # distance between coils with 25% overlap
    rotation = -(math.pi / 2)   # number of radians spiral is rotated
    maxTheta = radius / b   # after using formula r = b * theta
    
    theta = 0
    while theta <= maxTheta:
        # distance away from center
        away = b * theta
        
        # distance around center
        around = theta + rotation
        
        # around & away -> x & y
        x = centerX + math.cos(around) * away
        y = centerY + math.sin(around) * away
        
        # convert ECEF to NED and LLA
        n, e, d = ecef2ned(x, y, centerZ, lat, lon, altitude)
        newLat, newLon, newAlt = ned2geodetic(n, e, d, lat, lon, altitude)
        waypointsNED.append([n, e, d])
        waypointsLLA.append([newLat, newLon, newAlt])
        
        # generate a waypoint every pi/8 radian
        theta += configs["d_theta_rad"]
    
    return (waypointsNED, waypointsLLA)

def quick_scan_adds_mission(vehicle, lla_waypoint_list):
    """
    Adds a takeoff command and four waypoint commands to the current mission. 
    The waypoints are positioned to form a square of side length 2*aSize around the specified LocationGlobal (aLocation).

    The function assumes vehicle.commands matches the vehicle mission state 
    (you must have called download at least once in the session and after clearing the mission)
    """    

    cmds = vehicle.commands

    print(" Clear any existing commands")
    cmds.clear() 
    
    print(" Define/add new commands.")
    # Add new commands. The meaning/order of the parameters is documented in the Command class. 
    lat = 0
    lon = 1
     
    # Add MAV_CMD_NAV_TAKEOFF command. This is ignored if the vehicle is already in the air.
    # This command is there when vehicle is in AUTO mode, where it takes off through command list
    # In guided mode, the actual takeoff function is needed, in which case this command is ignored
    cmds.add(Command( 0, 0, 0, mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT, mavutil.mavlink.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, 0, 0, 10))


    #Define the four MAV_CMD_NAV_WAYPOINT locations and add the commands
    for point in lla_waypoint_list:
        cmds.add(Command( 0, 0, 0, mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT, mavutil.mavlink.MAV_CMD_NAV_WAYPOINT, 0, 0, 0, 0, 0, 0, point[0], point[1], 12))

    # adds dummy end point - this endpoint is the same as the last waypoint and lets us know we have reached destination and that
    cmds.add(Command( 0, 0, 0, mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT, mavutil.mavlink.MAV_CMD_NAV_WAYPOINT, 0, 0, 0, 0, 0, 0, lla_waypoint_list[-1][0], lla_waypoint_list[-1][1], 12))
    print("Upload new commands to vehicle")
    cmds.upload()


# Main autonomous flight thread
# :param configs: dict from configs file
# :param radio: XBee radio object
def quick_scan_autonomy(configs, radio):
    global xbee
    autonomy.xbee = radio
    comm_sim = None

    # If comms is simulated, start comm simulation thread
    if autonomy.xbee is None:
        comm_sim = Thread(target=comm_simulation, args=(configs["comms_simulated"]["comm_sim_file"], xbee_callback,))
        comm_sim.start()
    # Otherwise, import Xbee Python Library and set up callback
    else:
        from digi.xbee.devices import RemoteXBeeDevice, XBee64BitAddress
        autonomy.xbee.add_data_received_callback(xbee_callback)

    # Generate waypoints after start_mission = True
    while not autonomy.start_mission:
        pass

    # Generate waypoints
    waypoints = generateWaypoints(configs, search_area)
    num_points = len(waypoints[1])

    # Start SITL if vehicle is being simulated
    if (configs["vehicle_simulated"]):
        import dronekit_sitl
        sitl = dronekit_sitl.start_default(lat=1, lon=1)
        connection_string = sitl.connection_string()
    else:
        connection_string = "/dev/serial0"
        sitl = None

    # Connect to vehicle
    vehicle = connect(connection_string, wait_ready=True)

    # Send mission to vehicle
    quick_scan_adds_mission(vehicle, waypoints[1])

    # Takeoff
    takeoff(configs["flight_mode"], vehicle, configs["altitude"])

    vehicle.mode = VehicleMode(configs["flight_mode"])

    # Fly about spiral pattern
    if (configs["flight_mode"] == "AUTO"):
        while vehicle.commands.next != vehicle.commands.count:
            # TODO monitor pause, resume, stop variables
            if autonomy.pause_mission:
                configs["flight_mode"] = "HOVER" # real answer said QHOVER; depends on VehicleType?
            else:
                configs["flight_mode"] = "AUTO"
            if autonomy.stop_mission:
                break
            print(vehicle.location.global_frame)
            time.sleep(1)
    else:
        raise Exception("Guided mode not supported")

    land(vehicle)
    
    # Wait for comm simulation thread to end
    if comm_sim:
        comm_sim.join()

