import asyncio
import websockets
from threading import Thread
import docker
from docker.errors import NotFound
from ipaddress import ip_network
import os
import tempfile
import shutil
import threading
import subprocess
import time
import yaml
import uuid
from vncdotool import api
from PIL import Image
import numpy as np
from pathlib import Path
import filecmp
import pkg_resources
from functools import partial

class WebSocketServer:
    def __init__(self, host="127.0.0.1", port=39220):
        self.host = host
        self.port = port
        self.server = None
        self.clients = set()
        self.thread = None

    async def handler(self, websocket, path):
        # Register the client
        self.clients.add(websocket)
        client_ip = websocket.remote_address[0]
        print(f"New connection from {client_ip}")
        
        # Send a welcome message
        await websocket.send("Welcome to the WebSocket server!")
        
        try:
            # Keep handling messages from this client
            async for message in websocket:
                print(f"Received from {client_ip}: {message}")
        except websockets.exceptions.ConnectionClosed as e:
            print(f"Connection closed from {client_ip}: {e}")
        finally:
            # Unregister the client
            self.clients.remove(websocket)

    async def stop_server(self):
        print("Stopping WebSocket server...")
        if self.clients:
            # Notify and disconnect all clients
            await asyncio.gather(*(self.disconnect_client(client) for client in self.clients))
        self.server.close()
        await self.server.wait_closed()
        print("WebSocket server stopped.")

    async def disconnect_client(self, websocket):
        try:
            await websocket.send("END")
        except Exception as e:
            print(f"Error sending 'END' to client: {e}")
        finally:
            await websocket.close()

    def _start_server(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.start_server())
        self.loop.run_forever()

    async def start_server(self):
        # Start the WebSocket server using websockets.serve
        self.server = await websockets.serve(self.handler, self.host, self.port)
        print(f"WebSocket server running on ws://{self.host}:{self.port}")
        await self.server.wait_closed()

    def start(self):
        # Start the server in a separate thread
        self.thread = Thread(target=self._start_server, daemon=True)
        self.thread.start()

    def stop(self):
        if self.server:
            # Schedule the stop coroutine in the event loop
            asyncio.run_coroutine_threadsafe(self.stop_server(), self.loop)
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join()


class BrowserEnvironmentException(Exception):
    """Exception raised when maximum number of environments are created."""
    pass

class BrowserEnvironment:
    _docker_client = docker.from_env()
    _network_name = "browser_environment_network"
    _available_ips = []
    _lock = threading.Lock()  # For thread safety when managing IPs
    _initialized = False
    _ws_server = None
    
    # Class-level dictionary to map IP addresses to instances
    _instances = {}

    # Label to identify containers created by this script
    _container_label = "created_by=BrowserEnvironment"

    # Create Docker network and IP pool on the first instantiation
    @classmethod
    def _initialize_network(cls, ip_range="172.20.0.0/24"):
        """Initialize Docker network and populate available IP list."""
        try:
            cls._docker_client.networks.get(cls._network_name)
        except NotFound:
            # Create the network with a specified subnet
            cls._docker_client.networks.create(
                cls._network_name,
                driver="bridge",
                ipam=docker.types.IPAMConfig(
                    pool_configs=[docker.types.IPAMPool(subnet=ip_range)]
                )
            )

        # Generate list of available IPs from subnet, excluding gateway (.1)
        cls._available_ips = [str(ip) for ip in ip_network(ip_range).hosts()][1:]

    @classmethod
    def _cleanup_existing_containers(cls):
        """Stop and remove all containers created by previous runs of this script."""
        containers = cls._docker_client.containers.list(
            all=True,
            filters={"label": "created_by=BrowserEnvironment"}
        )
        for container in containers:
            try:
                container.stop()
                container.remove()
                print(f"Removed container {container.id}")
            except Exception as e:
                print(f"Failed to remove container {container.id}: {e}")

    def __init__(self):
        """Initialize the environment, start Docker container, and assign an IP."""


        config_template_path = Path(pkg_resources.resource_filename('browser_env', 'config-template'))
        extensions_path = Path(pkg_resources.resource_filename('browser_env', 'extensions'))
        compose_file = Path(pkg_resources.resource_filename('browser_env', 'compose.yaml'))
        
        # Perform one-time cleanup of existing containers
        if not BrowserEnvironment._initialized:
            BrowserEnvironment._cleanup_existing_containers()
            BrowserEnvironment._initialize_network()
            BrowserEnvironment._ws_server = WebSocketServer()
            BrowserEnvironment._ws_server.start()
            BrowserEnvironment._initialized = True

        # Acquire lock to safely assign IP
        with BrowserEnvironment._lock:
            if not BrowserEnvironment._available_ips:
                raise BrowserEnvironmentException("Maximum number of environments created")
            self.ip_address = BrowserEnvironment._available_ips.pop(0)

        # Store the instance in the class-level dictionary
        BrowserEnvironment._instances[self.ip_address] = self

        self.uid = os.getuid()  # Current user's UID
        self.gid = os.getgid()  # Current user's GID
        
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
                self._network_name: {
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
                "created_by": "BrowserEnvironment"
            }
        })

        self.compose_data['services']['browser_service']['environment'].update({
            "FF_OPEN_URL": "https://assets.standardrl.com/redirects/browser/?subnet=20",
            "VNC_PASSWORD": "12345",
            "USER_ID": self.uid,
            "GROUP_ID": self.gid,
            "KEEP_APP_RUNNING": "1"
        })

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
        
        self.vnc_client = None
        self._latest_screen = None
        self._known_mouse = None
            
    def _connect_vnc(self):
        self.vnc_client = api.connect(f"{self.ip_address}::5901", password="12345", timeout=1)
        while not self._known_mouse:
            try:
                self.vnc_client.mouseMove(0, 83)
                self._known_mouse = (0, 83)
            except:
                self.vnc_client = api.connect(f"{self.ip_address}::5901", password="12345", timeout=1)
                pass
                    
    def _set_screen(self, v):
        self._latest_screen = v

    def getScreen(self):
        """Capture and return a screenshot from the VNC connection."""
        if not self.vnc_client:
            self._connect_vnc()
        self.vnc_client.captureRegionPIL(self._set_screen, 0, 83, 500, 500)
        image = self._latest_screen.convert('RGB')
        rgba_array = np.array(image, dtype=np.uint8)
                    
        return rgba_array
    
    def nudgeMouse(self, dx, dy):
        if not self.vnc_client:
            self._connect_vnc()
        try:
            x, y = self._known_mouse
            newmouse = (min(max(0, x+dx), 499), min(max(0, y-83+dy), 499)+83)
            self.vnc_client.mouseMove(*newmouse)
            self._known_mouse = newmouse
            return True
        except Exception as e:
            return False

    def setMouse(self, x, y):
        """Set the mouse position on the VNC connection."""
        if not self.vnc_client:
            self._connect_vnc()
        try:
            newmouse = (min(max(0, x), 499), min(max(0, y), 499)+83)
            self.vnc_client.mouseMove(*newmouse)
            self._known_mouse = newmouse
            return True
        except Exception as e:
            return False

    def click(self, button=1):
        """Click the mouse button on the VNC connection."""
        if not self.vnc_client:
            self._connect_vnc()
        try:
            self.vnc_client.mousePress(button)
            return True
        except Exception as e:
            return False
    
    def mouseHoldStart(self, button=1):
        """Press the mouse button on the VNC connection."""
        if not self.vnc_client:
            self._connect_vnc()
        try:
            self.vnc_client.mouseDown(button)
            return True
        except Exception as e:
            return False

    def mouseHoldEnd(self, button=1):
        """Release the mouse button on the VNC connection."""
        if not self.vnc_client:
            self._connect_vnc()
        try:
            self.vnc_client.mouseUp(button)
            return True
        except Exception as e:
            return False
        
    def keyDown(self, key):
        """Release the mouse button on the VNC connection."""
        if not self.vnc_client:
            self._connect_vnc()
        try:
            self.vnc_client.keyDown(key)
            return True
        except Exception as e:
            return False
        
    def keyUp(self, key):
        """Release the mouse button on the VNC connection."""
        if not self.vnc_client:
            self._connect_vnc()
        try:
            self.vnc_client.keyUp(key)
            return True
        except Exception as e:
            return False
        
    def keyPress(self, key):
        """Release the mouse button on the VNC connection."""
        if not self.vnc_client:
            self._connect_vnc()
        try:
            self.vnc_client.keyPress(key)
            return True
        except Exception as e:
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
