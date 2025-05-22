![StandardRL Components Logo](https://assets.standardrl.com/general/components/icon-full.png)

**BrowserEnv: A Gymnasium-Compliant Reinforcement Learning Environment for Web Browsing**

*Bridge the gap between perception, language understanding, and action in open-ended web environments.* BrowserEnv empowers researchers to train RL agents that navigate, interact with, and extract information from live web pages in a fully controlled, reproducible setting. By combining Dockerized browser instances, VNC-based control, and a custom browser extension, BrowserEnv surfaces both low‑level pixel observations and rich, structured signals (DOM events, text selections, link graphs) to facilitate cutting-edge research in web interaction, question answering, and simulated user modeling.

---

## 🔍 Key Features & Research Capabilities

1. **Realistic Browser Interaction**

   * **Dockerized Firefox**: Each agent runs in an isolated Docker container (custom network, static IP) with a preconfigured profile (extensions, settings).
   * **VNC Control**: Agents issue mouse and keyboard commands via `vncdotool`, mirroring human-like pointing and typing behaviors.
   * **Parallel Environments**: Scale horizontally by spawning multiple containers on a shared Docker network with unique IPs.

2. **Multi-Modal Observations**

   * **Raw RGB Frames**: Full-page screenshots as NumPy arrays or PIL images (`getScreen()`), supporting pixel‑based perception and end‑to‑end deep learning.
   * **Zoomed-In Views**: Crop windows around the cursor (`convert_to_state`) for foveated vision experiments, reducing observation dimensionality.
   * **Browser Metadata**: Current URL, domain, and navigation history exposed in the `info` dictionary for auxiliary rewards or curriculum learning.

3. **Structured Event Streams**

   * **Click Events**: Text content and bounding rectangle of clicked DOM elements, enabling semantic grounding of actions.
   * **Text Selections**: Agent-driven highlights capture full sentences (cleaned of HTML), supporting reading comprehension and QA tasks.
   * **Link Discovery**: On page load, the extension enumerates all hyperlinks (`<a href>`), populating a domain‑URL graph for exploration and planning.
   * **Scroll & Hover Signals**: Delta scroll movements and link-hover notifications provide fine‑grained feedback on agent exploration strategies.

4. **Flexible Communication Channels**

   * **WebSockets** (default): Low-latency, bidirectional messaging between the browser extension and Python process.
   * **Named Pipes (FIFOs)**: Alternative IPC for headless or constrained environments, implemented via `os.mkfifo` and `struct`‑packed JSON frames.

5. **Gymnasium API Compliance**

   * **Action Spaces**:

     * *Relative Mode*: Discrete nine‑directional nudges + click/hold actions for joystick‑like control.
     * *Absolute Mode*: Cartesian `(x, y)` coordinate pairs for pixel‑precise pointing.
   * **Observation Spaces**: Configurable to output full RGB arrays (`Box(H, W, 3)`) or zoomed subregions (`Box(statewidth, stateheight, 3)`).
   * **Standard Methods**: Implements `reset(seed, options)`, `step(action)`, and `render()` following Gymnasium conventions.

6. **Reward Shaping & Metrics**

   * **Discovery Rewards**: +2 for visiting a novel domain, +1 for a previously unseen page within known domains.
   * **Interaction Incentives**: +1 for meaningful scroll actions or text highlights, encouraging active content engagement.
   * **Curriculum & Reset Strategies**: Optionally start from previously seen pages/domains (`reset_from_seen`) to bootstrap exploration.

---

## 🏗️ System Architecture

```text
+---------------------+        +------------------------+
| Python RL Process   | <----> | WebSocket/Named Pipe   |
| (BrowserGymEnv)     |        | Server (env.py)        |
+---------------------+        +------------------------+
        |                                   |
        v                                   v
+-----------------------------------------------+
| Docker Network: browser_environment_network   |
| +----------------------+   +----------------+ |
| | Container @ 172.X.Y.Z |<->| Firefox + VNC | |
| +----------------------+   +----------------+ |
+-----------------------------------------------+
        ^                                   |
        |                                   v
|-----------------------------------------------|
| Browser Extension                             |
| - background.js (WebSocket client)            |
| - content.js (DOM event capture & messaging)  |
|-----------------------------------------------|
```

* **Configuration**: `browser_env/compose.yaml` defines service image, VNC port, security options, and volume mounts for profiles and extensions.
* **Lifecycle Management**: `BrowserEnvironment` handles container startup (`docker-compose up`), network initialization, and clean teardown (`docker-compose down`).

---

## 🚀 Installation & Quick Start

```bash
# 1. Clone
git clone https://github.com/your-org/browser-env.git && cd browser-env

# 2. Python & Docker deps
pip install -r requirements.txt
docker pull jlesage/firefox

# 3. Load extension in Firefox Dev
about:debugging → Load Temporary Add-on → select Extension/manifest.json

# 4. Run a sample rollout
python - << 'EOF'
from browser_env.env import BrowserGymEnv

env = BrowserGymEnv(maxsteps=500, actionmode='relative', statemode='both', width=640, height=480)
obs, info = env.reset()
for _ in range(200):
    action = env.action_space.sample()
    obs, reward, done, _, info = env.step(action)
    if done:
        break
env.close()
EOF
```

---

## 📚 Research Applications & Extensions

* **Web Navigation Tasks**: Autonomous discovery and retrieval of target content under sparse rewards.
* **Reading Comprehension**: Agents highlight sentences to answer natural language queries using the selection feature.
* **Simulated User Behavior**: Model and analyze browsing strategies (e.g., goal‑directed vs. exploratory policies).
* **Hierarchical RL**: Leverage link graph signals for planning high‑level navigation goals.

**Customizability:**

* Modify reward functions in `BrowserGymEnv.step()`.
* Extend the browser extension to capture additional DOM events (forms, buttons, media).
* Integrate with language models by exposing selected text or page DOM snapshots as auxiliary inputs.

---

## 🎓 Citation

If BrowserEnv supports your work, please cite:

> **BrowserEnv: A Dockerized Gymnasium Environment for RL-Based Web Interaction**, 2025.

---

## ❤️ Contributing & License

Open issues, pull requests, and feature suggestions are welcome!
Distributed under the MIT License. See `LICENSE` for details.
