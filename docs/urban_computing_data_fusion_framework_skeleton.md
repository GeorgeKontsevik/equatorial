# Urban Computing / Cross-Domain Data Fusion Framing Skeleton

Working title:

> Weather-Perturbed Spatial Networks and Crop-Specific Accessibility Vulnerability in the Equatorial Belt

Alternative titles:

- Crop Accessibility Vulnerability in Weather-Perturbed Road Networks
- Cross-Domain Data Fusion for Equatorial Crop-to-Market Accessibility
- From Urban Computing to Regional Agricultural Accessibility Networks

This is a fill-in skeleton. It follows Zheng's cross-domain multimodal knowledge fusion logic, but adapts the final step from an AI task to a network science task.

## One-Sentence Positioning

This work frames equatorial agricultural accessibility as a cross-domain urban-computing problem: climate, transport infrastructure, crop geography, and logistics destinations are fused into attributed, weather-perturbed spatial road networks to quantify how weekly precipitation changes crop-specific access to cities, ports, and airports.

Shorter version:

> We study how weather-induced edge-weight perturbations in equatorial road networks propagate into crop-specific accessibility losses.

## Core Distinction From Zheng

Zheng's framework ends with an AI task. This study uses the same cross-domain fusion logic, but the final analytical object is not a learned AI model. It is an interpretable network science assessment.

Template text:

> Following the cross-domain knowledge fusion framework, we first identify the physical-world problem, its root causes, the factors that mediate it, and the datasets that encode those factors. Unlike the original framework, however, our final task is not supervised prediction or representation learning. The task is a deterministic network science computation: measuring how time-varying precipitation-sensitive edge weights alter shortest-path accessibility from crop production nodes to destination nodes.

## 1. Physical-World Problem

Problem statement:

> Agricultural production in equatorial countries depends on road access to local markets, larger urban centers, ports, and airports. Rainfall can slow or partially close road links, especially where roads are unpaved or surface quality is uncertain. The same rainfall regime does not produce the same accessibility impact for all crops, because crops are spatially distributed differently within each national road network.

Research question:

> How does the network position of crop production shape vulnerability to weather-induced road-network perturbations across equatorial countries?

More operational version:

> Given a set of equatorial country road networks with crop-production source nodes, destination nodes, road-surface attributes, and weekly precipitation-driven edge penalties, which countries, crops, and destination layers experience persistent or severe accessibility losses?

Fill-ins:

- Study region: [FILL: definition of equatorial belt / country inclusion rule]
- Year: 2024
- Current completed result scope: `cluster_connected_allclusters_10small_3large_3ports_3airports`
- Current scenario: `weekly_sum_penalty_v1`
- Current destination groups: 10 small cities, 3 large cities, 3 ports, 3 airports

## 2. Domains Layer: What To Fuse

Zheng sequence:

```text
problem -> root causes -> contributing factors -> relevant data -> sources/domains
```

For this project:

| Zheng step | This project |
| --- | --- |
| Problem | Crop-to-market/logistics accessibility can degrade under rainfall-sensitive road conditions. |
| Root causes | Precipitation-sensitive road speeds, unpaved/unknown surfaces, fragmented or weakly redundant road graphs, spatial mismatch between crop clusters and destination nodes. |
| Contributing factors | Weekly precipitation, road surface, road topology, crop production geography, destination hierarchy, baseline remoteness, route redundancy / critical corridors. |
| Relevant data | ERA5 precipitation, road-surface network, CROPGRIDS crop clusters, city/port/airport destinations, country boundaries, graph routing outputs. |
| Sources/domains | Climate reanalysis, transport infrastructure, agriculture, economic geography/logistics, administrative geography. |

Template paragraph:

> The target phenomenon cannot be observed from a single domain. Climate data identify where and when rainfall stress occurs, but not which crop routes are affected. Road-surface data identify potentially weather-sensitive links, but not which agricultural production areas depend on them. Crop production data identify agricultural source nodes, but not their connectivity to markets or logistics hubs. Destination data identify relevant economic targets, but not their network accessibility. The task therefore requires cross-domain fusion before any accessibility claim can be made.

## 3. Data Scarcity, Missingness, And Why Fusion Is Necessary

This is the section that should explicitly target Zheng's motivation: physical-world problems rarely have complete task-specific data.

Observed or project-relevant gaps:

- There are no direct labels for "true weekly crop accessibility loss" across equatorial countries.
- Road-surface coverage is incomplete or uncertain; unknown surfaces are analytically important.
- Flood-depth data are not available as a usable, consistent dynamic depth layer; binary flood extent is not equivalent to depth-based road failure.
- Several hazard variables exist as possible context or diagnostics, but the current production routing scenario applies precipitation penalties only.
- Crop production rasters do not directly provide network terminals; they must be transformed into representative crop-cluster source nodes.
- Road graphs are fragmented if raw components are used directly; crop origins may not lie on the main network component.
- Destination systems differ across countries, and some destination layers may be absent or sparse.

Template paragraph:

> This setting illustrates the data-insufficiency problem emphasized in cross-domain data fusion. The desired object of analysis, crop-specific weekly accessibility disruption, is not directly measured. It must be constructed from partial observations distributed across domains. The workflow therefore does not simply overlay datasets; it creates an explicit, reproducible representation in which climate cells, road segments, crop clusters, and destination nodes become comparable elements of a single temporal network problem.

Claim discipline:

- Do not claim a full multi-hazard model unless flood, heat, wind, visibility, and soil-moisture penalties are actually active in routing.
- The current routing scenario should be described as precipitation-driven or rainfall-sensitive.
- Flood-depth, heat, wind, dust, and soil moisture can be described as data-fusion context or future extension only unless used in the active result.

## 4. Links Layer: Why These Data Can Be Fused

Main alignment principle: dependency-based.

```text
weekly precipitation
-> road-surface-sensitive edge penalty
-> edge travel-time perturbation
-> shortest-path route cost
-> crop-origin-to-destination accessibility loss
-> country/crop/destination vulnerability profile
```

Secondary alignment principles:

- Multiview-based: climate, roads, crop geography, and destinations are different views of the same food-accessibility system.
- Similarity-based: countries, crops, or routes can later be compared by vulnerability regimes, but this is not the primary fusion mechanism.
- Commonality-based: transfer across countries is possible only after showing that the same network response mechanisms recur.

Template paragraph:

> The fusion is justified by explicit dependency links rather than by generic feature concatenation. Precipitation does not directly affect a crop cluster in the model. It affects road edges through surface-sensitive speed multipliers. Those edge-weight changes alter shortest-path travel times between crop nodes and destination nodes. Crop vulnerability is therefore a graph-mediated response to weather perturbations, not a direct raster exposure score.

Key links to describe:

| Link | Meaning | Transformation |
| --- | --- | --- |
| Road segment -> ERA5 cell | Assign climate forcing to transport edges. | Spatial point/probe or cell mapping. |
| Road segment -> surface group | Assign weather sensitivity to edges. | Surface classification: paved, unpaved, unknown, synthetic connector. |
| Crop raster -> crop terminal node | Turn production geography into source nodes. | Crop clustering and representative points. |
| Crop terminal -> road graph | Make crop production routable. | Cluster-connected graph terminals and connectors. |
| City/port/airport -> destination node | Turn economic/logistics targets into graph targets. | Nearest/snap-to-network destination tables. |
| Week -> edge weights | Turn climate time series into temporal graph costs. | Weekly rainfall penalties. |
| OD-week route -> accessibility metric | Measure network response. | A* shortest-path cost and baseline-relative delta. |

## 5. Data Layer: How Data Are Transformed

Zheng emphasizes structure, scale, resolution, and distribution. Use those exact categories.

### 5.1 Different Structures

Input structures:

- Raster/grid climate data.
- Vector road segments.
- Graph nodes and edges.
- Crop raster-derived clusters.
- Point destinations.
- Weekly result tables.

Template:

> The input datasets are not natively compatible. ERA5 is a gridded spatio-temporal climate product; road-surface data are vector line features; crop production is raster-derived and spatially clustered; cities, ports, and airports are point destinations; the final analytical object is a weighted graph. The workflow transforms these structures into a common network representation.

### 5.2 Different Spatial Resolutions

Examples:

- ERA5 precipitation grid: coarse climate cells.
- Road segments: line geometries at road-segment scale.
- Crop source points: representative cluster terminals.
- Destination nodes: snapped point targets.

Template:

> Resolution mismatch is handled by mapping climate cells to road edges and by converting crop production rasters into representative source nodes. This avoids treating raster cells, road segments, and destination points as directly comparable objects before transformation.

### 5.3 Different Temporal Resolutions

Current workflow:

- ERA5 hourly precipitation is aggregated into weekly precipitation metrics.
- The accessibility run uses 53 weeks in 2024.
- Baseline travel time is the best available week for the same OD pair.
- Delay is computed as `(weekly travel_time_h - baseline_h) * 60`.

Template:

> The temporal unit of analysis is the week. Hourly climate observations are aggregated into weekly forcing, and route costs are recomputed for each week. This converts climate variability into a temporal sequence of graph perturbations.

### 5.4 Different Distributions

Relevant distribution issues:

- Countries differ in graph size and density.
- Crops differ in the number and weight of clusters.
- Destination groups differ in availability and remoteness.
- Some countries/crops do not generate affected points above threshold.
- Absolute exposure weights can be dominated by large countries or high-production crops.

Template:

> Distributional differences are handled by reporting both absolute exposure and normalized response metrics. A large affected crop-cluster weight captures burden, while duration and mean delay capture vulnerability independent of total production scale.

## 6. Knowledge Fusion Paradigm

Use Zheng's distinction: precise fusion vs coarse fusion.

This project is precise fusion.

Template:

> The workflow follows a precise knowledge-fusion paradigm. Instead of learning latent embeddings, it extracts explicit objects and relations: road edges, surface groups, precipitation cells, crop terminal nodes, destination nodes, OD pairs, weekly edge weights, and shortest-path costs. The result is interpretable because every accessibility loss can be traced to a concrete source-destination pair, week, route-cost change, and set of edge attributes.

Why precise fusion is appropriate:

- The physical mechanism is sufficiently clear for an explicit model.
- There are not enough direct labels for supervised AI.
- Interpretability matters for infrastructure and food-system vulnerability claims.
- The task is to measure network response, not to infer a hidden representation.

What is not claimed:

> This is not a neural multimodal fusion model and does not claim AI prediction accuracy. It adapts the cross-domain data-fusion framing to a deterministic network science task.

## 7. Final Task: Network Science, Not AI

Formal task:

```text
Given:
  G_c,t = (V_c, E_c, w_e,t)
  c = country
  t = week
  S_c,k = crop source nodes for crop k
  D_c,g = destination nodes for destination group g

Compute:
  A(c, k, g, t) = accessibility from crop nodes to destination nodes
  Delta(c, k, g, t) = A(c, k, g, t) - baseline A(c, k, g)

Summarize:
  duration = number of affected weeks
  intensity = mean delay during affected weeks
  exposure = affected crop-cluster weight
  burden = duration x intensity x exposure or severe-delay sum
```

Template paragraph:

> The final task is to characterize the response of spatial infrastructure networks to weather perturbations. Each country is treated as an attributed road graph. Crop clusters are source nodes, cities/ports/airports are destination nodes, and precipitation changes edge weights through surface-sensitive penalty rules. The analysis asks how these perturbations propagate through shortest paths and which country-crop-destination combinations occupy persistent, severe, or high-exposure disruption regimes.

## 8. Current Empirical Scope From Produced Artifacts

Use only if still accurate when writing.

Current artifacts inspected:

- `outputs/astar_accessibility_weekly/cluster_connected_allclusters_10small_3large_3ports_3airports_plots/manifest.json`
- `outputs/astar_accessibility_weekly/cluster_connected_allclusters_10small_3large_3ports_3airports_delta_minutes_heatmaps/manifest.json`
- `outputs/astar_accessibility_weekly/visual_experiments/manifest.json`
- `outputs/astar_accessibility_weekly/visual_experiments/visual_experiment_cells.csv`
- `outputs/astar_accessibility_weekly/visual_experiments/visual_experiment_crop_points_cluster_weighted_by_dest.csv`

Current numbers:

- 29 countries rendered in A*/heatmap manifests.
- 53 weeks in 2024.
- 2,400,741 weekly A* result rows in the rendered manifests.
- 5 crops in the visual experiment: avocado, banana, mango, pineapple, plantain.
- 4 destination groups: airport, city_100k_plus, city_5_100k, port.
- 25,652 country-week-crop-destination cells in the visual experiment.
- 2,238 cells have median delay >= 3h; 1,207 have median delay >= 6h; 315 have median delay >= 24h.
- The destination-level duration-intensity summary has 416 affected points across 28 countries; BRN appears in cells but has no affected destination-crop point above the plotting threshold.

Current strongest observed regimes from the produced summary:

- Persistent port disruption is concentrated in COL and PNG crop-destination combinations.
- COL plantain/banana/pineapple/avocado to ports have 51 affected weeks and mean affected delays above 37h.
- PNG port access for several crops has 51 affected weeks and mean affected delays around 35-37h.
- Large-city access shows chronic disruption for PNG and COL, but with lower intensity than ports.
- Small-city access is generally less severe, though PNG and COL still produce high-duration points.
- Airport disruption is less uniformly chronic but has country-crop outliers such as MYS and LBR.

Use cautiously:

- `base_route_surface_mix` currently contains BDI only in the inspected export. Treat it as a mechanism prototype, not a region-wide result, until exported for all countries.

## 9. Suggested Figures And Tables

### Figure 1: Cross-Domain Fusion Flow

Purpose:

Show the adapted Zheng pipeline.

Layout:

```text
Physical-world problem
-> root causes
-> factors
-> domain datasets
-> explicit links
-> temporal weighted graph
-> network-science task
-> duration/intensity/exposure regimes
```

Caption skeleton:

> Adaptation of the cross-domain knowledge fusion framework to crop accessibility networks. The workflow does not end in a learned AI task; it produces a deterministic temporal graph assessment.

### Figure 2: Data Availability And Missingness

Purpose:

Make the "data scarcity" argument visible.

Rows:

- Climate forcing.
- Road surface.
- Crop clusters.
- Destinations.
- Flood depth.
- Visibility/dust.
- Soil moisture.
- Wind.

Columns:

- available;
- used in active routing;
- diagnostic only;
- unavailable / future extension;
- reason.

### Figure 3: Network Construction

Purpose:

Show how unlike data become one graph.

Elements:

- road edges;
- crop terminals;
- synthetic connectors;
- city/port/airport nodes;
- weekly edge weights.

### Figure 4: Duration-Intensity-Exposure Response

Use current plot:

- `07_crop_duration_intensity_crop_degradation.png`

Interpretation:

> x = disruption duration, y = disruption intensity, bubble = affected crop-cluster exposure, facet = destination layer, color = crop.

Possible improvements:

- Add regime lines, e.g. 26 weeks and 10h.
- Label only top burden points.
- Consider relative exposure share in addition to absolute exposure.

### Figure 5: Network Mechanism Plot

Needed for network science explanation.

Candidate x variables:

- share of baseline route length on unpaved/unknown/synthetic edges;
- share of baseline route travel time on weather-sensitive surfaces;
- baseline travel time / remoteness;
- path redundancy proxy;
- critical-corridor concentration.

Candidate y variables:

- annual severe burden;
- affected weeks;
- mean affected delay;
- probability of >=3h delay.

Caption skeleton:

> Relationship between network structure and accessibility response. Points are country-crop-destination combinations; x captures route exposure to weather-sensitive network structure, y captures disruption burden.

### Table 1: Domain Fusion Inventory

Columns:

- Domain.
- Dataset.
- Native structure.
- Native resolution.
- Transformation.
- Role in network task.
- Limitations.

### Table 2: Final Network Metrics

Rows:

- Baseline travel time.
- Weekly delta minutes.
- Affected week count.
- Mean affected delay.
- Peak delay.
- Annual severe burden.
- Affected crop-cluster exposure.
- Optional relative exposure share.

## 10. SOTA / Comparison Framing

Do not compare to multimodal AI leaderboards.

Compare against adjacent task classes:

1. Static accessibility products:
   - They estimate baseline access but not weekly weather-sensitive disruption.
2. Infrastructure climate-risk studies:
   - They often estimate exposed/damaged road assets, not crop-specific source-destination accessibility response.
3. Food-system logistics disruption studies:
   - They often focus on specific commodities or regions, not comparable cross-country crop-source network response.
4. Transport network robustness:
   - They analyze graph disruption, but often not with crop-specific production geography and destination hierarchy.

Template paragraph:

> The comparison is therefore not an accuracy leaderboard. The contribution is a task reframing: from static accessibility or asset exposure to crop-specific accessibility response in weather-perturbed spatial networks. The relevant baseline is what would be concluded under static travel times, crop-agnostic aggregation, surface-blind penalties, or country-level averages.

Suggested ablations:

- Static baseline vs weekly precipitation-penalized network.
- Crop-specific sources vs crop-agnostic or uniform source distribution.
- Destination-specific access vs pooled destinations.
- Surface-aware penalties vs surface-blind penalties.
- Cluster-connected graph vs exclusion of disconnected crop origins.
- Country aggregate vs crop-level response.

## 11. Main Claim To Defend

Strong version:

> Across equatorial countries, crop accessibility vulnerability is not reducible to rainfall exposure or national road density. It emerges from the interaction between crop-node placement, destination-node hierarchy, road-surface-attributed edge structure, and weekly precipitation-induced edge-weight perturbations.

Safer version:

> The produced results show that the same precipitation-sensitive road-network model yields different duration-intensity-exposure regimes across countries, crops, and destination groups, indicating that crop geography and network position mediate accessibility vulnerability.

## 12. What Not To Claim Yet

- Do not claim observed real-world road closures.
- Do not claim direct validation against actual shipment delays unless such data are added.
- Do not claim full food-security impact.
- Do not claim crop yield impact.
- Do not claim full multi-hazard risk if the active routing scenario is precipitation-only.
- Do not claim the synthetic connectors are real roads; describe them as graph-connectivity devices and check their lengths/sensitivity.
- Do not claim surface mix results are regional until `base_route_surface_mix` is exported for all countries.

## 13. Fill-In Paragraphs

### Abstract Fragment

> This paper studies crop-specific accessibility vulnerability in equatorial countries as a weather-perturbed network science problem. We fuse climate reanalysis, road-surface networks, crop production clusters, and destination layers into temporal weighted road graphs. Weekly precipitation modifies edge costs according to road-surface sensitivity, and A* routing estimates accessibility from crop nodes to small cities, large cities, ports, and airports. The resulting duration-intensity-exposure profiles reveal [FILL: main empirical finding], showing that [FILL: mechanism] rather than rainfall alone explains differential crop accessibility losses.

### Methods Opening

> We adapt the cross-domain knowledge fusion framework to a deterministic network task. The method first identifies causal factors behind crop accessibility loss, then selects datasets that encode those factors, then constructs explicit links among them through spatial mapping, graph construction, and temporal edge weighting. The final representation is a weekly sequence of attributed road networks rather than a learned latent representation.

### Results Opening

> The regional run covers [FILL] countries, [FILL] crop classes, [FILL] destination layers, and 53 weekly network states in 2024. We summarize each country-crop-destination response using duration, intensity, and affected exposure. This representation separates chronic mild disruptions from acute severe disruptions and identifies where large crop-cluster exposure coincides with persistent network degradation.

### Discussion Opening

> The results support a network-mediated interpretation of climate accessibility risk. Rainfall matters, but its impact depends on where crop production enters the network, which destination layer is considered, and whether critical routes rely on weather-sensitive road surfaces or weakly redundant corridors. This explains why country-level averages and static accessibility products can miss crop-specific vulnerability regimes.

## 14. Minimal Checklist Before Using In Text

- [ ] Confirm final country list.
- [ ] Confirm whether BRN is excluded from affected summaries only because no point crosses threshold.
- [ ] Confirm whether `weekly_sum_penalty_v1` uses weekly precipitation sum, hourly intensity, or another exact field in the active DB run.
- [ ] Confirm final crop source is CROPGRIDS, not SPAM, for the current result.
- [ ] Confirm how unknown surface is treated in the active routing run.
- [ ] Export route surface mix for all countries if using it as explanatory network mechanism.
- [ ] Decide whether bubble size is absolute affected cluster weight or relative affected exposure share.
- [ ] State clearly that baseline is the best observed week for the same OD pair.
- [ ] Inspect final PNGs before claiming final figure quality.

