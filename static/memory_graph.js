(function () {
  const graphOptions = {
    autoResize: true,
    groups: {
      understanding: {
        shape: "dot",
        color: {
          background: "#b45a64",
          border: "#93444e",
          highlight: { background: "#c96f78", border: "#7f3740" },
        },
        font: { color: "#2f2324" },
      },
      episode: {
        shape: "dot",
        color: {
          background: "#5b7d86",
          border: "#3f6670",
          highlight: { background: "#6f95a0", border: "#365a62" },
        },
        font: { color: "#2f2324" },
      },
      missing_episode: {
        shape: "triangle",
        color: {
          background: "#d9c9bb",
          border: "#9f8174",
          highlight: { background: "#e5d8cd", border: "#8b6b5f" },
        },
        font: { color: "#6e5753" },
      },
    },
    nodes: {
      borderWidth: 1,
      scaling: { min: 10, max: 34 },
      font: {
        face: "Outfit, PingFang SC, Hiragino Sans GB, sans-serif",
        size: 13,
        strokeWidth: 4,
        strokeColor: "rgba(255, 251, 247, 0.9)",
      },
    },
    edges: {
      color: { color: "rgba(113, 84, 74, 0.28)", highlight: "#b45a64" },
      width: 1,
      smooth: { type: "dynamic" },
      selectionWidth: 2,
    },
    interaction: {
      dragNodes: true,
      hover: true,
      multiselect: false,
      selectable: true,
      selectConnectedEdges: false,
      zoomView: true,
      dragView: true,
    },
    physics: {
      enabled: true,
      solver: "forceAtlas2Based",
      maxVelocity: 14,
      minVelocity: 0.45,
      adaptiveTimestep: true,
      forceAtlas2Based: {
        gravitationalConstant: -42,
        centralGravity: 0.006,
        springLength: 145,
        springConstant: 0.045,
        damping: 0.78,
        avoidOverlap: 0.45,
      },
      stabilization: { enabled: true, iterations: 260, fit: true },
    },
  };

  function isReady() {
    return Boolean(window.vis && window.vis.Network && window.vis.DataSet);
  }

  function clampZoom(value) {
    const scale = Number(value);
    if (!Number.isFinite(scale)) return 1;
    return Math.min(2.5, Math.max(0.2, scale));
  }

  function splitHistoryClauses(text) {
    const normalized = String(text || "").replace(/\s+/g, " ").trim();
    if (!normalized) return [];
    const clauses = normalized.match(/[^；;。！？!?，,\n]+[；;。！？!?，,]?/g);
    return (clauses || [normalized]).map(item => item.trim()).filter(Boolean);
  }

  function diffHistoryClauses(previousText, currentText) {
    const previous = splitHistoryClauses(previousText);
    const current = splitHistoryClauses(currentText);
    if (!previous.length) {
      return current.map(text => ({ type: "added", text }));
    }
    if (!current.length) return [];

    const dp = Array.from({ length: previous.length + 1 }, () =>
      Array(current.length + 1).fill(0),
    );
    for (let i = previous.length - 1; i >= 0; i -= 1) {
      for (let j = current.length - 1; j >= 0; j -= 1) {
        dp[i][j] =
          previous[i] === current[j]
            ? dp[i + 1][j + 1] + 1
            : Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }

    const diff = [];
    let i = 0;
    let j = 0;
    while (i < previous.length && j < current.length) {
      if (previous[i] === current[j]) {
        diff.push({ type: "same", text: current[j] });
        i += 1;
        j += 1;
      } else if (dp[i + 1][j] >= dp[i][j + 1]) {
        diff.push({ type: "removed", text: previous[i] });
        i += 1;
      } else {
        diff.push({ type: "added", text: current[j] });
        j += 1;
      }
    }
    while (i < previous.length) {
      diff.push({ type: "removed", text: previous[i] });
      i += 1;
    }
    while (j < current.length) {
      diff.push({ type: "added", text: current[j] });
      j += 1;
    }
    return diff;
  }

  function createNetwork({ container, nodes, edges, onSelectNode, onSelectEdge, onBlank, onZoom }) {
    if (!isReady()) {
      throw new Error("vis-network is not available");
    }
    const network = new window.vis.Network(
      container,
      {
        nodes: new window.vis.DataSet(nodes),
        edges: new window.vis.DataSet(edges),
      },
      graphOptions,
    );

    network.on("click", params => {
      const nodeId = params.nodes[0] || network.getNodeAt(params.pointer.DOM);
      if (nodeId && onSelectNode) {
        onSelectNode(nodeId);
        return;
      }
      const edgeId = params.edges[0] || network.getEdgeAt(params.pointer.DOM);
      if (edgeId && onSelectEdge) {
        onSelectEdge(edgeId);
        return;
      }
      if (onBlank) {
        onBlank();
      }
    });
    const fireZoom = scale => onZoom && onZoom(clampZoom(scale));
    network.on("zoom", params => fireZoom(params.scale));
    network.once("stabilizationIterationsDone", () => {
      network.stopSimulation();
      syncNetworkSize(network, container);
      fireZoom(network.getScale());
    });

    return network;
  }

  function syncNetworkSize(network, container) {
    network.setSize("100%", "100%");
    fitNetwork(network);
  }

  function fitNetwork(network) {
    network.redraw();
    network.fit({
      animation: { duration: 420, easingFunction: "easeInOutQuad" },
    });
  }

  function afterPaint(callback) {
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(callback);
    });
  }

  function createState() {
    return {
      memoryGraphOpen: false,
      memoryGraphLoading: false,
      memoryGraphError: "",
      memoryGraphAgent: "",
      memoryGraphAgents: [],
      memoryGraphStats: {},
      memoryGraphNodes: [],
      memoryGraphEdges: [],
      memoryGraphSelected: null,
      memoryGraphNetwork: null,
      memoryGraphZoom: 1,
      memoryGraphDetailCollapsed: false,

      async openMemoryGraph() {
        if (this.isCompact) {
          this.closeDrawer();
        }
        this.memoryGraphOpen = true;
        await this.$nextTick();
        await this.loadMemoryGraph(this.memoryGraphAgent || null);
      },

      closeMemoryGraph() {
        this.memoryGraphOpen = false;
        this.destroyMemoryGraphNetwork();
      },

      toggleMemoryGraphDetail() {
        this.memoryGraphDetailCollapsed = !this.memoryGraphDetailCollapsed;
      },

      destroyMemoryGraphNetwork() {
        if (!this.memoryGraphNetwork) return;
        this.memoryGraphNetwork.destroy();
        this.memoryGraphNetwork = null;
      },

      memoryGraphSubtitle() {
        if (this.memoryGraphError) return this.memoryGraphError;
        const agent = this.memoryGraphAgents.find(item => item.name === this.memoryGraphAgent);
        if (!agent) return "Understanding / Episode";
        return `${agent.display_name} · ${agent.understanding_count || 0} understandings · ${agent.episode_count || 0} episodes`;
      },

      memoryGraphEmptyText() {
        if (this.memoryGraphLoading) return "正在加载记忆图谱。";
        if (this.memoryGraphError) return this.memoryGraphError;
        if (!this.memoryGraphAgents.length) return "当前没有可展示的角色记忆。";
        return "当前角色还没有 understanding 或 episode。";
      },

      memoryGraphMetaPills() {
        const item = this.memoryGraphSelected;
        if (!item) return [];
        if (item.type === "edge") {
          const pills = [];
          if (item.episode_id) pills.push(`episode ${item.episode_id}`);
          if (item.understanding_id) pills.push(`understanding ${item.understanding_id}`);
          return pills;
        }
        const pills = [];
        if (item.location) pills.push(item.location);
        if (item.participants) pills.push(item.participants);
        if (Array.isArray(item.linked_episodes)) {
          pills.push(`${item.linked_episodes.length} linked episodes`);
        }
        (item.keywords || []).slice(0, 8).forEach(keyword => pills.push(keyword));
        return pills;
      },

      memoryGraphEpisodeFacts() {
        const item = this.memoryGraphSelected;
        if (!item || !["episode", "edge"].includes(item.type)) return [];
        const source = item.type === "edge" ? item.episode : item;
        if (!source) return [];
        const facts = [];
        const date = String(source.date || "").trim();
        const time = String(source.time || "").trim();
        const dateLabel =
          date && time ? (time.startsWith(date) ? time : `${date} · ${time}`) : date || time;
        if (dateLabel) facts.push(dateLabel);
        if (source.importance) facts.push(`importance ${source.importance}`);
        return facts;
      },

      memoryGraphHistoryHeading(entry) {
        if (!entry) return "";
        const date = (entry.date || "").trim();
        const title = (entry.title || "").trim();
        return [date, title].filter(Boolean).join(" · ");
      },

      memoryGraphHistoryEntries() {
        const item = this.memoryGraphSelected;
        if (!item || !Array.isArray(item.history)) return [];
        return item.history
          .filter(entry => String(entry.content || "").trim())
          .map((entry, index, entries) => ({
            ...entry,
            diff: diffHistoryClauses(index > 0 ? entries[index - 1].content : "", entry.content),
          }))
          .reverse();
      },

      memoryGraphEdgeDiffSegments() {
        const item = this.memoryGraphSelected;
        if (!item || item.type !== "edge" || !Array.isArray(item.diff)) return [];
        return item.diff;
      },

      async loadMemoryGraph(agentName = null) {
        this.memoryGraphLoading = true;
        this.memoryGraphError = "";
        this.memoryGraphSelected = null;
        this.memoryGraphZoom = 1;
        this.destroyMemoryGraphNetwork();

        try {
          const query = agentName
            ? `?${new URLSearchParams({ agent: agentName }).toString()}`
            : "";
          const response = await this.fetchJson(`/api/memory-graph${query}`);
          this.memoryGraphAgents = response.agents || [];
          this.memoryGraphAgent = response.selected_agent || agentName || "";
          this.memoryGraphStats = response.stats || {};
          this.memoryGraphNodes = response.nodes || [];
          this.memoryGraphEdges = response.edges || [];
        } catch (error) {
          this.memoryGraphError = "记忆图谱加载失败。";
          this.memoryGraphStats = {};
          this.memoryGraphNodes = [];
          this.memoryGraphEdges = [];
        } finally {
          this.memoryGraphLoading = false;
        }

        await this.$nextTick();
        this.renderMemoryGraphNetwork();
      },

      renderMemoryGraphNetwork() {
        if (!this.memoryGraphOpen || this.memoryGraphLoading || this.memoryGraphError) return;
        if (!this.memoryGraphNodes.length || !this.$refs.memoryGraphNetwork) return;
        if (!isReady()) {
          this.memoryGraphError = "vis-network 未加载。";
          return;
        }

        this.destroyMemoryGraphNetwork();
        this.memoryGraphNetwork = createNetwork({
          container: this.$refs.memoryGraphNetwork,
          nodes: this.memoryGraphNodes,
          edges: this.memoryGraphEdges,
          onSelectNode: nodeId => this.selectMemoryGraphNode(nodeId),
          onSelectEdge: edgeId => this.selectMemoryGraphEdge(edgeId),
          onBlank: () => {
            this.memoryGraphDetailCollapsed = true;
          },
          onZoom: scale => {
            if (scale !== this.memoryGraphZoom) this.memoryGraphZoom = scale;
          },
        });
        afterPaint(() => {
          if (this.memoryGraphNetwork) {
            syncNetworkSize(this.memoryGraphNetwork, this.$refs.memoryGraphNetwork);
          }
        });
        if (this.memoryGraphNodes[0]) {
          this.selectMemoryGraphNode(this.memoryGraphNodes[0].id);
        }
      },

      selectMemoryGraphNode(nodeId) {
        const node = this.memoryGraphNodes.find(item => item.id === nodeId);
        this.memoryGraphSelected = node ? node.meta : null;
        if (node) {
          this.memoryGraphDetailCollapsed = false;
        }
      },

      selectMemoryGraphEdge(edgeId) {
        const edge = this.memoryGraphEdges.find(item => item.id === edgeId);
        if (!edge) {
          this.memoryGraphSelected = null;
          return;
        }
        const fromNode = this.memoryGraphNodes.find(item => item.id === edge.from);
        const toNode = this.memoryGraphNodes.find(item => item.id === edge.to);
        const candidates = [fromNode, toNode].filter(Boolean);
        const episodeNode = candidates.find(item =>
          ["episode", "missing_episode"].includes(item.meta && item.meta.type),
        );
        const understandingNode = candidates.find(item => item.meta && item.meta.type === "understanding");
        if (!episodeNode || !understandingNode) {
          this.memoryGraphSelected = null;
          return;
        }

        const episode = episodeNode.meta;
        const understanding = understandingNode.meta;
        const episodeId = (edge.meta && edge.meta.episode_id) || episode.id;
        const historyEntries = Array.isArray(understanding.history)
          ? understanding.history.filter(entry => String(entry.content || "").trim())
          : [];
        const historyIndex = historyEntries.findIndex(
          entry => String(entry.episode_id || "") === String(episodeId || ""),
        );
        const historyEntry = historyIndex >= 0 ? historyEntries[historyIndex] : null;
        const diff = historyEntry
          ? diffHistoryClauses(
              historyIndex > 0 ? historyEntries[historyIndex - 1].content : "",
              historyEntry.content,
            )
          : [];
        const hasVisibleDiff = diff.some(segment => segment.type !== "same");

        this.memoryGraphSelected = {
          id: edge.id,
          type: "edge",
          type_label: "Episode Impact",
          title: [episode.title, understanding.title].filter(Boolean).join(" / "),
          episode,
          understanding,
          episode_id: episodeId,
          understanding_id: (edge.meta && edge.meta.understanding_id) || understanding.id,
          history_entry: historyEntry,
          diff: hasVisibleDiff ? diff : [],
        };
        this.memoryGraphDetailCollapsed = false;
      },

      setMemoryGraphZoom(value) {
        if (!this.memoryGraphNetwork) return;
        const scale = clampZoom(value);
        this.memoryGraphZoom = scale;
        this.memoryGraphNetwork.moveTo({ scale, animation: false });
      },
    };
  }

  window.agentGalMemoryGraph = {
    createState,
    createNetwork,
    isReady,
  };
})();
