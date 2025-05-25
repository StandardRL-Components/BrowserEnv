import asyncio
import json
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import uuid

from ipaddress import ip_network
from pathlib import Path
from threading import Condition, Thread

import docker
from docker.errors import NotFound
from gymnasium import spaces
import gymnasium as gym
import random
import websockets
import yaml
from PIL import Image
import numpy as np
from vncdotool import api
import pkg_resources
from urllib.parse import urlparse


class NamedPipeServer:
    """
    Server for inter-process communication using named FIFO pipes.

    Waits for incoming JSON messages on a receiver pipe and forwards
    parsed messages to a target object's receive() method. Allows sending
    JSON messages on a sender pipe.
    """

    def __init__(self):
        self.receiver_pipe = None
        self.sender_pipe = None
        self.server_thread = None
        self.running = False

    def startServer(self, receiver_pipe_name: str, sender_pipe_name: str, target: any):
        """
        Initialize and start the named-pipe server.

        Ensures FIFO files exist, sets the target message handler,
        and spawns a background thread to listen for incoming messages.
        """
        self.receiver_pipe = receiver_pipe_name
        self.sender_pipe = sender_pipe_name
        self.target = target

        # Create FIFOs if they do not exist
        for pipe_name in (self.receiver_pipe, self.sender_pipe):
            if not os.path.exists(pipe_name):
                os.mkfifo(pipe_name)

        # Launch the listening thread
        self.running = True
        self.server_thread = threading.Thread(target=self._listen, daemon=True)
        self.server_thread.start()

    def _listen(self):
        """
        Loop reading messages from the receiver FIFO.

        Each message is prefixed with a 4-byte size header. On receiving
        a complete JSON payload, parse and dispatch it to the target.
        """
        while self.running:
            try:
                with open(self.receiver_pipe, 'rb') as pipe:
                    while True:
                        header = pipe.read(4)
                        if not header:
                            break

                        length = struct.unpack('I', header)[0]
                        data = pipe.read(length)
                        if len(data) != length:
                            continue

                        message = json.loads(data.decode('utf-8'))
                        self.target.receive(message)
            except Exception as e:
                print("NamedPipeServer listen error:", e)

    def sendMessage(self, message: dict):
        """
        Send a JSON-formatted message through the sender FIFO.

        Serializes the dict, prepends a 4-byte size header, and writes
        to the sender pipe.
        """
        try:
            payload = json.dumps(message).encode('utf-8')
            header = struct.pack('I', len(payload))
            with open(self.sender_pipe, 'wb') as pipe:
                pipe.write(header + payload)
        except Exception as e:
            print("NamedPipeServer sendMessage error:", e)

    def stopServer(self):
        """
        Gracefully stop the listening thread and terminate the server.
        """
        self.running = False
        if self.server_thread:
            self.server_thread.join()


class WebSocketServer:
    """
    WebSocket-based communication server for relaying messages
    between BrowserEnvironment instances and browser extensions.
    """

    def __init__(self, host="127.0.0.1", port=39220):
        self.host = host
        self.port = port
        self.server = None
        self.clients = set()
        self.thread = None
        self.loop = None

    async def handler(self, websocket):
        """
        Coroutine handling each client connection.

        Registers new clients, relays incoming JSON messages to the
        corresponding BrowserEnvironment instance, and cleans up on disconnect.
        """
        # Track the client and determine its IP address
        self.clients.add(websocket)
        headers = websocket.request_headers
        client_ip = (headers.get("X-Forwarded-For", websocket.remote_address[0])
                     .split(",")[0].strip())
        print(f"WebSocket connection from {client_ip}")

        # Subscribe the client websocket to its environment
        if client_ip in BrowserEnvironment._instances:
            BrowserEnvironment._instances[client_ip].websockets.add(websocket)

        # Acknowledge connection
        await websocket.send(json.dumps({"type": "connection", "ip": client_ip}))

        try:
            async for raw in websocket:
                message = json.loads(raw)
                if client_ip in BrowserEnvironment._instances:
                    BrowserEnvironment._instances[client_ip].receive(message)
        except websockets.exceptions.ConnectionClosed as e:
            print(f"WebSocket closed from {client_ip}: {e}")
        finally:
            # Cleanup client state
            self.clients.discard(websocket)
            if client_ip in BrowserEnvironment._instances:
                BrowserEnvironment._instances[client_ip].websockets.discard(websocket)

    async def start_server(self):
        """
        Create and run the WebSocket server on the configured host and port.
        """
        self.server = await websockets.serve(self.handler, self.host, self.port)
        print(f"WebSocket server listening on ws://{self.host}:{self.port}")
        await self.server.wait_closed()

    def _run_loop(self):
        """
        Internal thread method to initialize an asyncio event loop
        and start the server coroutine.
        """
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.start_server())
        self.loop.run_forever()

    def start(self):
        """
        Launch the WebSocket server in a background daemon thread.
        """
        self.thread = Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    async def _stop_server(self):
        """
        Coroutine to close all client connections and the server socket.
        """
        # Notify clients of shutdown
        for ws in list(self.clients):
            try:
                await ws.send(json.dumps({"type": "END"}))
                await ws.close()
            except:
                pass
        self.server.close()
        await self.server.wait_closed()

    def stop(self):
        """
        Stop the WebSocket server and join the thread.
        """
        if self.server and self.loop:
            asyncio.run_coroutine_threadsafe(self._stop_server(), self.loop)
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join()


class DummyBrowserEnv:
    """
    Placeholder environment to route messages from Manager to
    NamedPipeServer or WebSocketServer without launching a real browser.
    """

    def __init__(self, manager, ip):
        self.manager = manager
        self.ip = ip
        self.websockets = set()

    def receive(self, msg):
        """
        Forward received messages back through the manager's sendTo()
        method using the stored IP tag.
        """
        self.manager.sendTo(self.ip, msg)


class BrowserEnvironmentManager:
    """
    Orchestrates multiple BrowserEnvironment instances by managing
    IPC channels and IP assignment in a Docker network.
    """

    def __init__(self):
        self.directory = {}        # Map of tag -> pipe metadata
        self.master_pipe = None
        self.listener_thread = None
        self.running = False
        self.subnet = 20

    def generatePipe(self):
        """
        Create a new named FIFO for a BrowserEnvironment instance.

        Returns a unique tag (IP) and the FIFO path. Initializes
        global network and WebSocket server on first invocation.
        """
        if not BrowserEnvironment._initialized:
            BrowserEnvironment._cleanup_existing_containers()
            BrowserEnvironment._initialize_network()
            BrowserEnvironment._ws_server = WebSocketServer(port=39200 + self.subnet)
            BrowserEnvironment._ws_server.start()
            BrowserEnvironment._initialized = True

        # Allocate next available IP address as tag
        with BrowserEnvironment._lock:
            if not BrowserEnvironment._available_ips:
                raise BrowserEnvironmentException("Max environments created")
            tag = BrowserEnvironment._available_ips.pop(0)

        pipe_path = f"/tmp/{tag}_pipe"
        if not os.path.exists(pipe_path):
            os.mkfifo(pipe_path)

        self.directory[tag] = {"open": False, "path": pipe_path, "pipe": None}
        BrowserEnvironment._instances[tag] = DummyBrowserEnv(self, tag)

        return tag, pipe_path

    def startListening(self) -> str:
        """
        Set up a master FIFO to listen for environment-wide events.

        Returns the master pipe path.
        """
        self.master_pipe = "/tmp/master_pipe"
        if not os.path.exists(self.master_pipe):
            os.mkfifo(self.master_pipe)

        self.running = True
        self.listener_thread = threading.Thread(target=self._listen, daemon=True)
        self.listener_thread.start()
        return self.master_pipe

    def _listen(self):
        """
        Continuously read messages from the master FIFO and broadcast
        them to the appropriate environment instance over WebSockets.
        """
        while self.running:
            try:
                with open(self.master_pipe, 'rb') as pipe:
                    while True:
                        header = pipe.read(4)
                        if not header:
                            break
                        length = struct.unpack('I', header)[0]
                        data = pipe.read(length)
                        if len(data) != length:
                            continue
                        message = json.loads(data.decode('utf-8'))
                        ip = message.get('ip')
                        if ip in BrowserEnvironment._instances:
                            websockets.broadcast(
                                BrowserEnvironment._instances[ip].websockets,
                                json.dumps(message)
                            )
            except Exception as e:
                print("Manager listen error:", e)

    def sendTo(self, tag: str, msg: dict):
        """
        Send a JSON message to a specific FIFO identified by tag.

        Opens pipe on first send, writes size header + payload.
        """
        if tag not in self.directory:
            print(f"No pipe for tag {tag}")
            return
        info = self.directory[tag]
        try:
            payload = json.dumps(msg).encode('utf-8')
            header = struct.pack('I', len(payload))
            if not info['open']:
                info['pipe'] = open(info['path'], 'wb')
                info['open'] = True
            info['pipe'].write(header + payload)
        except Exception as e:
            print(f"Manager sendTo error for {tag}:", e)

    def stopListening(self):
        """
        Terminate the listening thread on the master FIFO.
        """
        self.running = False
        if self.listener_thread:
            self.listener_thread.join()


class BrowserEnvironmentException(Exception):
    """Exception when environment pool capacity is exceeded."""
    pass


class BrowserEnvironment:
    """
    Encapsulates a headless browser session within a Docker container.

    Exposes methods for navigation, input events via VNC, and
    IPC integration to stream observations back to an agent.
    """
    _docker_client = docker.from_env()
    _network_name = None
    _available_ips = []
    _lock = threading.Lock()
    _initialized = False
    _ws_server = None
    _nm_server = None
    _subnet = None
    visited_links = set()
    seen_links = {}
    _instances = {}

    @classmethod
    def _initialize_network(cls, ip_range="172.20.0.0/24"):
        """
        Create or load a Docker bridge network with the given subnet,
        and populate the available IP address pool.
        """
        try:
            cls._docker_client.networks.get(cls._network_name)
        except NotFound:
            cls._docker_client.networks.create(
                cls._network_name,
                driver="bridge",
                ipam=docker.types.IPAMConfig(
                    pool_configs=[docker.types.IPAMPool(subnet=ip_range)]
                )
            )
        cls._available_ips = [str(ip) for ip in ip_network(ip_range).hosts()][1:]

    @classmethod
    def _cleanup_existing_containers(cls):
        """
        Stop and remove Docker containers labeled as created_by this class.
        """
        label = f"created_by=BrowserEnvironment{cls._subnet}"
        containers = cls._docker_client.containers.list(all=True, filters={"label": label})
        for c in containers:
            try:
                c.stop()
                c.remove()
            except:
                pass

    def __init__(self, height, width, subnet=20, send_pipe=None, recv_pipe=None, child_mode=False, static_ip=None):
        """Initialize the environment, start Docker container, and assign an IP."""

        config_template_path = Path(pkg_resources.resource_filename('browser_env', 'config-template'))
        extensions_path = Path(pkg_resources.resource_filename('browser_env', 'extensions'))
        compose_file = Path(pkg_resources.resource_filename('browser_env', 'compose.yaml'))
        
        self.subnet = subnet

        # Perform one-time cleanup of existing containers
        if not BrowserEnvironment._initialized:
            BrowserEnvironment._subnet = self.subnet
            BrowserEnvironment._network_name = f"browser_environment_network_{self.subnet}"
            if not child_mode:
                BrowserEnvironment._cleanup_existing_containers()
                BrowserEnvironment._initialize_network(ip_range=f"172.{self.subnet}.0.0/24")
            if send_pipe is None or recv_pipe is None:
                BrowserEnvironment._ws_server = WebSocketServer(port=39200+self.subnet)
                BrowserEnvironment._ws_server.start()
            else:
                BrowserEnvironment._nm_server = NamedPipeServer()
                BrowserEnvironment._nm_server.startServer(recv_pipe, send_pipe, self)
            BrowserEnvironment._initialized = True

        if static_ip is None:
            # Acquire lock to safely assign IP
            with BrowserEnvironment._lock:
                if not BrowserEnvironment._available_ips:
                    raise BrowserEnvironmentException("Maximum number of environments created")
                self.ip_address = BrowserEnvironment._available_ips.pop(0)
        else:
            self.ip_address = static_ip

        # Store the instance in the class-level dictionary
        BrowserEnvironment._instances[self.ip_address] = self

        self.uid = os.getuid()  # Current user's UID
        self.gid = os.getgid()  # Current user's GID

        self.websockets = set()

        self.TOOLBAR_MARGIN = 87

        self.last_seen = time.time()
        self.last_msg = None

        self.width = width
        self.height = height

        self.isMouseDown = False

        self.current_url = ""

        self.last_element = None
        self.last_highlight = None
        self.last_scroll = None

        self.vnc_client = None

        self._mouseUpCondition = Condition()
        self._mouseDownCondition = Condition()
        self._navigateCondition = Condition()
        self._mouseMoveCondition = Condition()
        self._connectionCondition = Condition()
        
        # Create a unique directory for this container's config based on IP
        self.config_dir = tempfile.mkdtemp()

        # Generate a unique project name to isolate this instance
        self.project_name = f"browser_env_{uuid.uuid4().hex[:8]}"

        # Modify the compose file to set the correct volumes, IP address, and exposed ports
        with open(compose_file, "r") as file:
            self.compose_data = yaml.safe_load(file)

        os.mkdir(os.path.join(self.config_dir, "profile"))
        for f in os.listdir(os.path.join(config_template_path, 'profile')):
            if not os.path.isdir(os.path.join(config_template_path, 'profile', f)):
                shutil.copyfile(os.path.join(config_template_path, 'profile', f), os.path.join(self.config_dir, 'profile', f))

        # Apply environment-specific configurations to the compose file data
        self.compose_data['services']['browser_service'].update({
            "networks": {
                BrowserEnvironment._network_name: {
                    "ipv4_address": self.ip_address
                }
            },
            "volumes": [
                f"{os.path.abspath(self.config_dir)}:/config",
                f"{os.path.abspath(extensions_path)}:/config/profile/extensions:ro",
                f"{os.path.abspath(config_template_path / '/profile/settings')}:/config/profile/settings:ro",
                #f"{os.path.abspath(config_template_path / '/profile/prefs.js')}:/config/profile/prefs.js:ro",
            ],
            "expose": ["5901"],  # Expose port 5901 for VNC
            "labels": {  # Add label to identify containers created by this script
                "created_by": f"BrowserEnvironment{self.subnet}"
            }
        })

        self.compose_data['services']['browser_service']['environment'].update({
            "FF_OPEN_URL": f"https://assets.standardrl.com/redirects/ff/?subnet={self.subnet}",
            "VNC_PASSWORD": "12345",
            "USER_ID": self.uid,
            "GROUP_ID": self.gid,
            "KEEP_APP_RUNNING": "1",
            "DISPLAY_WIDTH": self.width,
            "DISPLAY_HEIGHT": self.TOOLBAR_MARGIN+self.height
        })

        self.compose_data['networks'] = {}
        self.compose_data['networks'][BrowserEnvironment._network_name] = {
            'external': True
        }

        print(self.compose_data)
        
        
        # Write modified compose data to a temporary file for this instance
        self.modified_compose_file = tempfile.NamedTemporaryFile(delete=False, suffix=".yaml")
        with open(self.modified_compose_file.name, 'w') as file:
            yaml.dump(self.compose_data, file)

        self.modified_compose_file.close()
        # Start the container with the modified compose file and unique project name
        try:
            subprocess.run(
                ["docker-compose", "-f", self.modified_compose_file.name, "up", "-d"],
                check=True,
                env=dict(os.environ, COMPOSE_PROJECT_NAME=self.project_name)
            )
        except subprocess.CalledProcessError as e:
            # Return IP to pool and clean up temp dir if container fails to start
            with BrowserEnvironment._lock:
                BrowserEnvironment._available_ips.append(self.ip_address)
                
            # Remove the instance from the class-level dictionary
            BrowserEnvironment._instances.pop(self.ip_address, None)

            if self.config_dir and os.path.isdir(self.config_dir):
                shutil.rmtree(self.config_dir)

            os.remove(self.modified_compose_file.name)
            raise e
        
        self._latest_screen = None
        self._known_mouse = None
        self.recent_events = []
        self.link_hover = False

    def navigate(self, url):
        if BrowserEnvironment._ws_server:
            websockets.broadcast(self.websockets, json.dumps({"type":"navigation", "url": url}))
        elif BrowserEnvironment._nm_server:
            BrowserEnvironment._nm_server.sendMessage({"ip": self.ip_address, "type":"navigation", "url": url})
            
    def _connect_vnc(self):
        self.vnc_client = api.connect(f"{self.ip_address}::5901", password="12345", timeout=1)
        while not self._known_mouse:
            try:
                self.vnc_client.mouseMove(2, self.TOOLBAR_MARGIN+2)
                self._known_mouse = (2, self.TOOLBAR_MARGIN+2)
            except:
                self.vnc_client = api.connect(f"{self.ip_address}::5901", password="12345", timeout=1)
                pass
                    
    def _set_screen(self, v):
        self._latest_screen = v

    def receive(self, msg):

        self.last_seen = time.time()
        self.last_msg = msg
        if msg["type"] == "mouseup":
            with self._mouseUpCondition:
                self._mouseUpCondition.notify()
            return
        elif msg["type"] == "mousedown":
            with self._mouseDownCondition:
                self._mouseDownCondition.notify()
            return
        elif msg["type"] == "navigate":
            with self._navigateCondition:
                self._navigateCondition.notify()
        elif msg["type"] == "linkhover":
            self.link_hover = True
        elif msg["type"] == "mousemove":
            with self._mouseMoveCondition:
                self._mouseMoveCondition.notify()
            self.link_suggestion = msg["suggestion"]
            return
        elif msg["type"] == "connection":
            with self._connectionCondition:
                self._connectionCondition.notify()
            return
        elif msg["type"] == "navigation":
            cururl = msg["url"]
            if "https://assets.standardrl.com" in cururl:
                cururl = "<Internal URL>"
            else:   
                BrowserEnvironment.visited_links.add(cururl)
            #if self.current_url == cururl:
            #    # Doesn't count if it's not a new page
            #    return
            self.current_url = cururl
        elif msg["type"] == "links":
            for k in msg["value"]:
                domain = urlparse(k).netloc
                if domain not in BrowserEnvironment.seen_links.keys():
                    if not k.startswith("about"):
                        BrowserEnvironment.seen_links[domain] = [k]
                else:
                    if not k.startswith("about"):
                        BrowserEnvironment.seen_links[domain].append(k)
            return
        elif msg["type"] == "click":
            self.last_element = {"text": msg["text"], "rect": msg["bounding_rect"]}
        elif msg["type"] == "highlight":
            self.last_highlight = " ".join(msg["value"])
        elif msg["type"] == "scroll":
            self.last_scroll = {"x": msg['value_x'], "y": msg['value_y']}

        self.recent_events.append(msg)

    def clearEvents(self):
        self.recent_events = []

    def awaitMouseUp(self):
        with self._mouseUpCondition:
            if self._mouseUpCondition.wait(timeout=2):
                return
            else:
                return

    def awaitMouseDown(self):
        with self._mouseDownCondition:
            if self._mouseDownCondition.wait(timeout=2):
                return
            else:
                return
            
    def awaitMouseMove(self):
        with self._mouseMoveCondition:
            if self._mouseMoveCondition.wait(timeout=2):
                return
            else:
                return

    def awaitNavigate(self):
        with self._navigateCondition:
            if self._navigateCondition.wait(timeout=2):
                return
            else:
                return

    def awaitConnection(self):
        with self._connectionCondition:
            if self._connectionCondition.wait(timeout=2):
                return
            else:
                return

    def getBlankScreen(self, mode="rgb_array"):
        return np.zeros((self.height, self.width, 3), dtype=np.int8)

    def getScreen(self, mode="rgb_array"):
        """Capture and return a screenshot from the VNC connection."""
        if not self.vnc_client:
            self._connect_vnc()

        try:
            self.vnc_client.captureRegionPIL(self._set_screen, 0, self.TOOLBAR_MARGIN, self.width, self.height)

            if mode == "pil":
                return self._latest_screen
            elif mode == "rgb_array":
                image = self._latest_screen.convert('RGB')
                rgba_array = np.array(image, dtype=np.uint8)
                return rgba_array
            else:
                return None
        except Exception as e:
            self.vnc_client = None
            return np.zeros((self.height, self.width, 3), dtype=np.int8)
    
    def nudgeMouse(self, dx, dy):
        if not self.vnc_client:
            self._connect_vnc()
        try:
            x, y = self._known_mouse
            newmouse = (int(min(max(0, x+dx), self.width-1)), int(min(max(0, y-self.TOOLBAR_MARGIN+dy), self.height-1)+self.TOOLBAR_MARGIN))
            self.vnc_client.mouseMove(*newmouse)
            self._known_mouse = newmouse
            return True
        except Exception as e:
            self.vnc_client = None
            return False

    def setMouse(self, x, y):
        """Set the mouse position on the VNC connection."""
        if not self.vnc_client:
            self._connect_vnc()
        try:
            newmouse = (int(min(max(0, x), self.width-1)), int(min(max(0, y), self.height-1)+self.TOOLBAR_MARGIN))
            self.vnc_client.mouseMove(*newmouse)
            self._known_mouse = newmouse
            return True
        except Exception as e:
            self.vnc_client = None
            return False

    def click(self, button=1):
        """Click the mouse button on the VNC connection."""
        if not self.vnc_client:
            self._connect_vnc()
        try:
            self.vnc_client.mousePress(button)
            return True
        except Exception as e:
            self.vnc_client = None
            return False
    
    def mouseHoldStart(self, button=1):
        """Press the mouse button on the VNC connection."""
        if not self.vnc_client:
            self._connect_vnc()
        try:
            self.vnc_client.mouseDown(button)
            self.isMouseDown = True
            return True
        except Exception as e:
            self.vnc_client = None
            return False

    def mouseHoldEnd(self, button=1):
        """Release the mouse button on the VNC connection."""
        if not self.vnc_client:
            self._connect_vnc()
        try:
            self.vnc_client.mouseUp(button)
            self.isMouseDown = False
            return True
        except Exception as e:
            self.vnc_client = None
            return False
        
    def keyDown(self, key):
        """Release the mouse button on the VNC connection."""
        if not self.vnc_client:
            self._connect_vnc()
        try:
            self.vnc_client.keyDown(key)
            return True
        except Exception as e:
            self.vnc_client = None
            return False
        
    def keyUp(self, key):
        """Release the mouse button on the VNC connection."""
        if not self.vnc_client:
            self._connect_vnc()
        try:
            self.vnc_client.keyUp(key)
            return True
        except Exception as e:
            self.vnc_client = None
            return False
        
    def keyPress(self, key):
        """Release the mouse button on the VNC connection."""
        if not self.vnc_client:
            self._connect_vnc()
        try:
            self.vnc_client.keyPress(key)
            return True
        except Exception as e:
            self.vnc_client = None
            return False
        
        
    def close(self):
        """Stop and remove the container, return the IP address to the pool, delete the temp directory, and close VNC."""
        try:
            subprocess.run(
                ["docker-compose", "-f", self.modified_compose_file.name, "down"],
                check=True,
                env=dict(os.environ, COMPOSE_PROJECT_NAME=self.project_name)
            )
        except subprocess.CalledProcessError as e:
            print(f"Error stopping container: {e}")

        # Close the VNC connection if it's open
        if self.vnc_client:
            self.vnc_client.disconnect()

        # Release the IP back to the pool and clean up resources
        with BrowserEnvironment._lock:
            BrowserEnvironment._available_ips.append(self.ip_address)
            
        # Remove the instance from the class-level dictionary
        BrowserEnvironment._instances.pop(self.ip_address, None)

        if self.config_dir and os.path.isdir(self.config_dir):
            shutil.rmtree(self.config_dir)
            
        # Remove the modified compose file if it exists
        try:
            if os.path.exists(self.modified_compose_file.name):
                os.remove(self.modified_compose_file.name)
        except PermissionError as e:
            print(f"Permission error when trying to delete file {self.modified_compose_file.name}: {e}")

    def __del__(self):
        """Ensure the container is closed, IP returned, and VNC disconnected on object deletion."""
        self.close()


class BrowserGymEnv(gym.Env):
    """
    Gymnasium-compatible wrapper exposing BrowserEnvironment
    as an RL environment with configurable action and observation spaces.
    """
    metadata = {'render_modes': ['rgb_array']}
    _all_time_seen = {}

   def __init__(self, maxsteps=5000, actionmode='relative', reset_from_seen=False, width=500, height=500, statemode='full', statewidth=100, stateheight=100, subnet=20, runtime_args=None, cautious_mode=False):
        super(BrowserGymEnv, self).__init__()
        
        self.cautious_mode = cautious_mode
        self.reset_from_seen = reset_from_seen

        if not runtime_args:
            runtime_args = {}

        # Initialize the ParticleSimulation with given parameters
        self.browser = BrowserEnvironment(height=height, width=width, subnet=subnet, **runtime_args)
        
        self.maxsteps = maxsteps

        self.statewidth = statewidth
        self.stateheight = stateheight

        self.actionmode = actionmode
        self.statemode = statemode

        self.rewarded_urls = set()
        self._all_time_seen_domains = set()

        self.last_url = None

        # Define the action space: (hit, x, y)
        if self.actionmode == "relative":
            self.action_space = spaces.Discrete(9)
        elif self.actionmode == "absolute":
            self.action_space = spaces.Tuple((
                spaces.Discrete(width), 
                spaces.Discrete(height)
            ))
        else:
            raise Exception("Unknown action type, use 'relative' or 'absolute'")

        if self.statemode == "full":
            self.observation_space = spaces.Box(low=0, high=255, shape=(width, height, 3), dtype=np.uint8)
        elif self.statemode == "zoomed":
            self.observation_space = spaces.Box(low=0, high=255, shape=(self.statewidth, self.stateheight, 3), dtype=np.uint8)
        elif self.statemode == "both":
            print("WARNING: Using 'both' mode means that observation_space will not be set")
        else:
            raise Exception("Unknown state type, use 'full' or 'zoomed'")

        
        # Rendering options
        self.render_mode = None
        self.width = width
        self.height = height

        self.reset()

    def convert_to_state(self, data, x, y):
        height, width, channels = data.shape

        # Define cropping bounds
        left = x - self.statewidth // 2
        right = x + self.statewidth // 2
        top = y - self.stateheight // 2
        bottom = y + self.stateheight // 2

        # Create a blank image for the output
        cropped_image = np.zeros((self.stateheight, self.statewidth, channels), dtype=data.dtype)

        # Calculate the region to copy from the original image
        src_x1 = max(0, left)
        src_x2 = min(width, right)
        src_y1 = max(0, top)
        src_y2 = min(height, bottom)

        # Calculate the corresponding region in the output image
        dst_x1 = max(0, -left)
        dst_x2 = self.statewidth - max(0, right - width)
        dst_y1 = max(0, -top)
        dst_y2 = self.stateheight - max(0, bottom - height)

        # Copy the valid region from the original image to the output image
        cropped_image[dst_y1:dst_y2, dst_x1:dst_x2] = data[src_y1:src_y2, src_x1:src_x2]

        return cropped_image
    
    def _getState(self):

        if self.statemode == "zoomed":
            state = self.convert_to_state(self.browser.getScreen(), self.browser._known_mouse[0], self.browser._known_mouse[1]-self.browser.TOOLBAR_MARGIN)
        elif self.statemode == "full":
            state = self.browser.getScreen()
        elif self.statemode == "both":
            state = self.browser.getScreen(), self.convert_to_state(self.browser.getScreen(), self.browser._known_mouse[0], self.browser._known_mouse[1]-self.browser.TOOLBAR_MARGIN)

        return state

    def reset(self, seed=None, options=None):
        """
        Reset the simulation to start over.
        """
        # Seeding
        super().reset(seed=seed)
        
        self.stepcount = 0

        if time.time()-self.browser.last_seen > 10:
            # Force restart the browser if we are stuck
            print("")
            print(f"Time since last seen too high: {time.time()-self.browser.last_seen}s ago")
            print(self.browser.last_msg)
            print("")
            self.browser.keyDown("ctrl")
            self.browser.keyPress("w")
            self.browser.keyUp("ctrl")
            self.browser.awaitConnection()

        self.rewarded_urls = set()

        self.last_url = None

        if self.reset_from_seen:
            if len(BrowserEnvironment.seen_links.keys()) > 0:
                rdomain = random.choice(list(BrowserEnvironment.seen_links.keys()))
                start_url = random.choice(BrowserEnvironment.seen_links[rdomain])
                self.browser.navigate(start_url)
                print("Starting at ",start_url)
                self.browser.awaitNavigate()
        else:
            if len(self._all_time_seen.keys()) > 0:
                rdomain = random.choice(list(self._all_time_seen.keys()))
                start_url = random.choice(self._all_time_seen[rdomain])
                self.browser.navigate(start_url)
                print("Starting at ",start_url)
                self.browser.awaitNavigate()

        self.browser.setMouse(random.randrange(0, self.browser.height), random.randrange(0, self.browser.width))

        return self._getState(), {}

    def step(self, action):
        """
        Execute the given action in the simulation environment.
        """

        if self.stepcount > self.maxsteps:
            return

        self.stepcount += 1

        if self.actionmode == "absolute":
            x, y = action
            self.browser.clearEvents()
            self.browser.setMouse(x, y)
            self.browser.click()
            self.browser.awaitMouseUp()

        elif self.actionmode == "relative":
            delta = 20
            proceed = False
            if action == 0:
                self.browser.nudgeMouse(delta, 0)
            elif action == 1:
                self.browser.nudgeMouse(delta/2, delta/2)
            elif action == 2:
                self.browser.nudgeMouse(0, delta)
            elif action == 3:
                self.browser.nudgeMouse(-delta/2, delta/2)
            elif action == 4:
                self.browser.nudgeMouse(-delta, 0)
            elif action == 5:
                self.browser.nudgeMouse(-delta/2, -delta/2)
            elif action == 6:
                self.browser.nudgeMouse(0, -delta)
            elif action == 7:
                self.browser.nudgeMouse(delta/2, -delta/2)
            elif action == 8:
                self.browser.click()
                self.browser.awaitMouseUp()
                proceed = True
            elif action == 9:
                if self.browser.isMouseDown:
                    self.browser.mouseHoldEnd()
                    self.browser.awaitMouseUp()
                else:
                    self.browser.mouseHoldStart()
                    self.browser.awaitMouseDown()
                proceed = True

            if self.cautious_mode and not proceed:
                self.browser.awaitMouseMove()
            
        lastel = self.browser.last_element
        self.browser.last_element = None

        lasthi = self.browser.last_highlight
        self.browser.last_highlight = None

        lastscr = self.browser.last_scroll
        self.browser.last_scroll = None

        linkhov = self.browser.link_hover
        self.browser.link_hover = False

        events = self.browser.recent_events
        self.browser.clearEvents()
        reward = 0

        did_navigate = False
        new_page = False

        for e in events:
            if e["type"] == "navigation":
                if "https://assets.standardrl.com" not in e["url"]:
                    did_navigate = True
                    if e["url"] not in self.rewarded_urls:
                        new_page = True

                if e["url"] not in self.rewarded_urls and "https://assets.standardrl.com" not in e["url"]:
                    domain = urlparse(e["url"]).netloc
                    if domain not in self._all_time_seen_domains:
                        reward = 2
                        self._all_time_seen_domains.add(domain)
                    else:
                        reward = 1
                    if self.stepcount == 1:
                        reward = 0
                    else:
                        if reward == 2:
                            print(f"New site ({domain}) seen by {self.browser.ip_address}")
                        elif reward == 1:
                            print(f"New page seen by {self.browser.ip_address}")

                    self.rewarded_urls.add(e["url"])
                    if self.last_url is not None:
                        last_domain = urlparse(self.last_url).netloc
                        if last_domain not in self._all_time_seen.keys():
                            if not self.last_url.startswith("about"):
                                self._all_time_seen[last_domain] = [self.last_url]
                                print(f"Just been to {last_domain} for the first time")
                                print(f"Now been to {len(self._all_time_seen.keys())} domains")
                        else:
                            if not self.last_url.startswith("about"):
                                self._all_time_seen[last_domain].append(self.last_url)
                    self.last_url = e["url"]
                    break
            elif e["type"] == "scroll":
                if (e["value_x"] > 0 and e["value_y"] >= 0) or (e["value_y"] > 0 and e["value_x"] >= 0):
                    reward = 1
                    break
            elif e["type"] == "highlight":
                if len(e["value"]) > 1:
                    reward = 1
                    break

        if self.stepcount > self.maxsteps:
            print(f"{self.browser.ip_address} finished")
            done = True
        else:
            done = False
            
        
        return self._getState(), reward, done, False, {"current_url": self.browser.current_url, "did_navigate": did_navigate, "new_page": new_page, "last_element": lastel, "last_highlight": lasthi, "last_scroll": lastscr, "mouse_held": self.browser.isMouseDown, "link_hover": linkhov}

            
    def render(self):
        return self.browser.getScreen()
