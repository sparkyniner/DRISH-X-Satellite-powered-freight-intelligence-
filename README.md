<div align="center">

# DrishX

**See what's moving. Anywhere. For free.**

Automated vehicle traffic intelligence from Sentinel-2 satellite imagery.

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Sentinel-2](https://img.shields.io/badge/Sentinel--2-Copernicus-003399?style=flat-square&logo=europeanunion&logoColor=white)](https://dataspace.copernicus.eu)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)
[![LinkedIn](https://img.shields.io/badge/Sairaj_Balaji-0A66C2?style=flat-square&logo=linkedin&logoColor=white)](https://www.linkedin.com/in/sairaj-balaji-7295b2246/)

[Quick Start](#quick-start) | [Use Cases](#use-cases) | [How It Works](#how-it-works) | [Targets](#interesting-targets)

---

</div>
<img width="1714" height="1070" alt="Image" src="https://github.com/user-attachments/assets/5ddbc0d3-05d8-4058-bde1-3e2f60572e02" />

## What is this?

DrishX answers a simple question for any road on Earth: **how much traffic is on it, and how has that changed over time?**

It works by exploiting a quirk in how the Sentinel-2 satellite captures imagery. The sensor records red, green, and blue light 1.01 seconds apart. Anything stationary looks normal. But a vehicle moving at highway speed shifts position between those captures, leaving a distinctive blue-green-red spectral smear across a few pixels. DrishX finds those smears, counts them, estimates their speed and direction, and tracks how volume changes across weeks and months.

The output is a traffic activity time-series for any major road corridor on the planet. Built on completely free Copernicus data, runs locally in a browser, and requires zero ground infrastructure.

## Why this matters

Traffic on roads is one of the most reliable observable indicators of what is actually happening in a place. More useful than official statements. Harder to fake than statistics. And until now, getting systematic road traffic data for an arbitrary location required either expensive commercial providers or physical access to install sensors.

DrishX changes that. Point it at any road, anywhere, and get months of traffic trend data in minutes.

The applications are as broad as the question "what's moving on this road" is broad. Anywhere that vehicle activity tells you something meaningful, DrishX can give you a data-driven answer from orbit.

## Use Cases

### Economic Intelligence

Truck traffic is one of the most honest economic signals that exists. When a port's throughput drops, you see it in the vehicles leaving the gate before any press release. When trade routes shift, road traffic moves before the official statistics do. DrishX gives you a proxy for economic activity that updates every 5 days and covers any corridor on Earth, from the Shahid Rajaee port highway in Bandar Abbas to the Mombasa-Nairobi A109 in Kenya.

### Supply Chain and Logistics

Monitor corridor congestion without relying on your own fleet data. Validate traffic projections for new facility locations by checking 6 months of satellite data instead of trusting a consultant's estimate. Benchmark seasonal patterns across competing routes. Identify bottlenecks by comparing volume across segments of the same corridor.

### Trade and Sanctions Monitoring

When sanctions take effect or tariffs change, the impact shows up on road corridors before it shows up in trade databases. Watch the Laredo-Nuevo Laredo I-35 crossing for US-Mexico rerouting signals. Monitor port feeder roads for throughput changes. Compare parallel corridors to spot where traffic is diverting.

### Security and Defense Intelligence

Vehicle movement on roads near sensitive facilities, military installations, border crossings, and restricted zones is a meaningful observable. DrishX can detect changes in traffic volume and patterns on access roads, supply routes, and perimeter corridors over time. This includes roads serving military bases, nuclear facilities, missile test sites, naval ports, and border staging areas.

To be clear about what this means in practice: DrishX can tell you that vehicle activity on a specific road increased by 40% over the past two weeks, or that a normally busy corridor has gone quiet. It cannot identify what the vehicles are. At 10m resolution, a military truck looks identical to a civilian truck. You cannot distinguish a tank transporter from a logging truck. You cannot read markings, count axles, or determine cargo. What you get is volume, speed, heading, and trend. That is a useful signal when combined with other sources and context, but it is not a surveillance system and should not be presented as one.

The same limitations apply to nuclear or WMD monitoring. You can observe whether traffic patterns on access roads to known facilities have changed. You cannot determine what is being transported. The intelligence value is in the pattern and the change, not in the individual detection.


### Disaster and Crisis Response

After floods, earthquakes, or conflict, which roads are actually operational? DrishX can compare current vehicle activity against a historical baseline to identify corridors that have gone quiet (blocked, damaged) or corridors carrying unusual volume (diversion routes, evacuation flows). Especially useful in areas with poor real-time reporting infrastructure.


### Journalism and Investigations

Need evidence that does not come from a press release? DrishX gives you satellite-derived, timestamped, independently verifiable data. When officials claim a trade corridor is thriving, you can check. When a new road is supposedly complete, you can see if anyone is actually using it. The data comes from a European Space Agency satellite, not from any government or corporation with a stake in the answer.

## Quick Start

### Prerequisites

- Python 3.11 (other verisons might not work)
- A free [Copernicus Data Space](https://dataspace.copernicus.eu/) account (takes 2 minutes)
- The trained RF model file (in the drishx sub directory)
- Be patient it takes time soemtimes pinging mirrors for the road data

### Install and run

```bash
# Clone the repository
git clone https://github.com/sparkyniner/DRISH-X-Satellite-powered-freight-intelligence-.git
cd DRISH-X-Satellite-powered-freight-intelligence-
 
# Create and activate a virtual environment
python3.11 -m venv venv
 
# On macOS / Linux:
source venv/bin/activate
 
# On Windows:
venv\Scripts\activate

cd DrishX
 
# Install dependencies
pip install -r requirements.txt
 
# Start the server
python drishx.py
```
 
The server starts on port 8000. Open your browser and go to:
 
```
http://localhost:8000
```

Open `http://localhost:8000`. On first launch a connect window opens automatically — enter your Client ID and Client Secret to link the satellite API (you can also open it anytime via **Connect** on the **Copernicus Link** card in the sidebar). That is it. No config files needed.

If you prefer environment variables instead of the UI, you can copy `.env.example` to `.env` and fill in your keys there. Both methods work. The connect window is just easier for most people.

## Using the Interface

DrishX is designed to be used directly from the browser with minimal setup.

### Selecting an Area of Interest (AOI)

- Right-click anywhere on the map  
- Click **“Draw AOI”**  
- Drag to define your analysis region  

The system will automatically:
- detect road corridors in the selected area  
- fetch satellite data  
- begin analysis  

---

### Recommended Settings

For best results:

- **Mode:** Use **Dense Mode** for maximum data coverage  
- **Timeline:** Adjust based on your use case:
  - Short (1–2 weeks) → quick signals  
  - Medium (1–3 months) → trend detection  
  - Long (3–6 months) → strong baseline + anomaly detection  

Dense mode increases the number of images processed, improving detection reliability, especially in regions with cloud cover.



### Data Storage

By default, all cached data and detection outputs go to `drishx_data/` in the project directory. To redirect (for example, to an external drive with more space):

```bash
export DRISHX_DATA_DIR=/path/to/your/storage
```

### Environment Variables

<img width="1702" height="1073" alt="Image" src="https://github.com/user-attachments/assets/4a131e82-db54-474d-829e-1e4582eed27d" />
These are optional if you connect through the UI instead.

| Variable | Required | Description |
|---|---|---|
| `COPERNICUS_CLIENT_ID` | Yes (or use UI) | Copernicus Data Space OAuth client ID |
| `COPERNICUS_CLIENT_SECRET` | Yes (or use UI) | Copernicus Data Space OAuth client secret |
| `RF_MODEL_PATH` | No | Path to trained RF model (defaults to `./rf_model.pickle`) |
| `DRISHX_DATA_DIR` | No | Root directory for all data (defaults to `./drishx_data`) |

## How It Works

Based on [Fisser et al. (2022)](https://ui.adsabs.harvard.edu/abs/2022RemS...14.1595F/abstract), adapted for real-time web streaming.

### The Physics

Sentinel-2's sensor captures spectral bands at slightly different times, about 1.01 seconds between blue (B02) and red (B04). A vehicle at 80 km/h moves roughly 22 meters in that interval. At 10m pixel resolution, it shows up at different positions in each band, creating a blue to green to red smear that DrishX is trained to find.

### The Pipeline

```
Sentinel-2 Image (10m resolution, 5-day revisit)
    |
    +-- 1. Feature Stack (7 features per pixel)
    |     Variance of RGB, Normalized ratio R/B, Normalized ratio G/B,
    |     Mean-centered B04, B03, B02, B08
    |
    +-- 2. Random Forest Classification
    |     Each pixel classified as: background, blue, green, or red
    |     Post-process: threshold background confidence at 0.75
    |
    +-- 3. Recursive Object Extraction
    |     Start at blue pixels, grow through green, then red
    |     Validate: all 3 colors present, 3-5 pixel extent
    |     Score: mean_max_prob + mean_prob > 1.2
    |
    +-- 4. Per-Detection Output
          Lat/lon, heading, speed estimate, confidence score
```

### Capabilities and Limits

<img width="1700" height="1075" alt="Image" src="https://github.com/user-attachments/assets/c9c32a92-9fe8-4bc9-b728-9e997096f456" />
You can also compare trends and historical data between areas


**What it does well:**

- Count large vehicles (trucks, buses) on major highways, roughly 70-80% detection rate with the trained model on European motorways
- Track volume trends over weeks and months
- Estimate speed (plus or minus 15 km/h) and heading (plus or minus 22.5 degrees) per detection
- Cover anywhere on Earth with Sentinel-2 imagery
- Process multi-month archives in minutes with parallel analysis

**What it cannot do:**

- Detect cars (they are smaller than one pixel at 10m resolution)
- Distinguish vehicle types. A military convoy looks the same as a line of delivery trucks. A fuel tanker looks the same as a water tanker. You get "large vehicle," nothing more.
- See through clouds (optical satellite limitation)
- Provide real-time monitoring. Sentinel-2 revisits every 5 days, and imagery is available with a delay. This is a trend analysis tool, not a live feed.
- Guarantee uniform accuracy globally. Best results on dark asphalt in clear conditions. Weaker on light-colored or unpaved roads, and in frequently cloudy regions.


## Interesting Targets

### Built-in Validation Sites

Pre-configured from the original S2TruckDetect research:

| Site | Highway | Bbox | Notes |
|---|---|---|---|
| Braunschweig | A7 | `52.25, 10.45, 52.32, 10.55` | Research-grade validation |
| Frankfurt | A3 | `50.05, 8.55, 50.12, 8.65` | High-density corridor |
| Karlsruhe | A5 | `48.95, 8.35, 49.05, 8.45` | Standard benchmark |

### Worth Investigating

Use "Draw AOI" on the map view. Not built-in, but they produce strong results.

**Chokepoints and disruption indicators:**
- Shahid Rajaee Highway, Bandar Abbas, Iran. 70% of Iran's container trade.
- Bandar Abbas to Sirjan Road (Highway 71). Primary northbound artery from Iran's main port.
- Rotterdam A15. Europe's busiest port feeder highway.
- Laredo I-35, Texas. Largest US-Mexico freight crossing.

**Rerouting and diversion signals:**
- Durban N3, South Africa. Captures Cape of Good Hope rerouting traffic.
- Chabahar port roads, Iran. Alternative that bypasses Hormuz. Inverse signal to Bandar Abbas.

**Economic proxies:**
- Mombasa-Nairobi A109, Kenya. Carries nearly all East African imports.
- UAE E11, Abu Dhabi to Dubai. Excellent imaging conditions, high traffic, good accuracy benchmark.
- Gwadar-Quetta M8, Pakistan. CPEC corridor activity.

**Activity pattern monitoring:**
- Access roads to any facility, installation, or zone where changes in vehicle volume over time are a meaningful signal. Historical comparison is the key capability here. A single observation tells you little. A trend over 6 months tells you a lot.

## Project Structure

```
drishx/
+-- drishx.py              # Backend: API + detection engine
+-- rf_model.pickle        # Trained RF model (not in repo)
+-- .env                   # Credentials (not in repo, optional if using UI)
+-- requirements.txt       # Dependencies
+-- frontend/
|   +-- index.html         # Dashboard
|   +-- app.js             # Frontend logic
|   +-- styles.css         # Styling
+-- drishx_data/           # Auto-created
    +-- sentinel_data/
    |   +-- detections/    # Vehicle crop images
    +-- osm_cache/         # OpenStreetMap cache
    +-- sh_cache/          # SentinelHub cache
```

### API

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/analyze` | Run detection on an AOI (streaming progress) |
| `GET` | `/api/roads` | Fetch road network for a bbox |
| `GET` | `/api/sites` | List preset and historical sites |
| `GET` | `/api/feed` | Recent detection alerts |
| `GET` | `/api/analytics/trends` | Daily counts aggregated across missions |
| `GET` | `/api/detections/:id` | Detections for a specific mission |

## Technical Notes

### Resolution

10m pixels. A truck (roughly 18m) spans about 2 pixels. The motion smear extends it to 3-5 pixels, which is enough for reliable detection. Cars (roughly 4.5m) are sub-pixel and invisible. If you need smaller vehicles, the upgrade path is PlanetScope at 3.7m, which uses the same spectral physics but requires a Planet Labs API key.

### Cloud Cover

DrishX uses the Sentinel-2 cloud mask to exclude cloudy pixels. Overcast frames show zero detections. That is correct behavior. Focus on the moving average trend rather than individual days.

### Regional Accuracy

Trained on German autobahns. In practice:

- European motorways: roughly 70-80% detection, 5-10% false positives
- Middle East and North Africa: strong, arid, high-contrast roads, rarely cloudy
- South and Southeast Asia: mixed, monsoon season limits usable frames
- Sub-Saharan Africa: good on paved trunk roads, weak on unpaved



## References

Fisser, H., Rahimi, E., Tetteh, M., Hoeser, T., Mayer-Gurr, T., and Kunzer, C. [Detecting Moving Trucks on Roads Using Sentinel-2 Data](https://ui.adsabs.harvard.edu/abs/2022RemS...14.1595F/abstract). Remote Sensing of Environment, 2022.

Reference implementation: [S2TruckDetect](https://ui.adsabs.harvard.edu/abs/2022RemS...14.1595F/abstract) by Henrik Fisser.

Satellite data: [Copernicus Data Space Ecosystem](https://dataspace.copernicus.eu/) (free, ESA).
Roads: [OpenStreetMap](https://www.openstreetmap.org/) via Overpass API.

## License

MIT.



---

<div align="center">



*DrishX, from the Sanskrit drishti: sight, vision, perspective.*

[Sairaj Balaji](https://www.linkedin.com/in/sairaj-balaji-7295b2246/)

</div>
