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
      selectable: false,
      selectConnectedEdges: false,
      zoomView: true,
      dragView: true,
    },
    physics: {
      enabled: true,
      solver: "forceAtlas2Based",
      forceAtlas2Based: {
        gravitationalConstant: -62,
        centralGravity: 0.012,
        springLength: 120,
        springConstant: 0.08,
        avoidOverlap: 0.45,
      },
      stabilization: { enabled: true, iterations: 160, fit: true },
    },
  };

  function isReady() {
    return Boolean(window.vis && window.vis.Network && window.vis.DataSet);
  }

  function createNetwork({ container, nodes, edges, onSelect }) {
    if (!isReady()) {
      throw new Error("vis-network is not available");
    }
    ensureContainerSize(container);

    const network = new window.vis.Network(
      container,
      {
        nodes: new window.vis.DataSet(nodes),
        edges: new window.vis.DataSet(edges),
      },
      graphOptions,
    );

    network.on("click", params => {
      if (params.nodes.length && onSelect) {
        onSelect(params.nodes[0]);
      }
    });
    network.once("stabilizationIterationsDone", () => {
      syncNetworkSize(network, container);
    });

    return network;
  }

  function ensureContainerSize(container) {
    const rect = container.getBoundingClientRect();
    if (rect.height < 120) {
      container.style.height = "480px";
    }
  }

  function syncNetworkSize(network, container) {
    const rect = container.getBoundingClientRect();
    const width = Math.max(320, Math.floor(rect.width || container.clientWidth || 0));
    const height = Math.max(360, Math.floor(rect.height || container.clientHeight || 0));
    network.setSize(`${width}px`, `${height}px`);
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
        const pills = [];
        if (item.id) pills.push(item.id);
        if (item.date) pills.push(item.time ? `${item.date} ${item.time}` : item.date);
        if (item.location) pills.push(item.location);
        if (item.participants) pills.push(item.participants);
        if (item.importance) pills.push(`importance ${item.importance}`);
        if (Array.isArray(item.linked_episodes)) {
          pills.push(`${item.linked_episodes.length} linked episodes`);
        }
        (item.keywords || []).slice(0, 8).forEach(keyword => pills.push(keyword));
        return pills;
      },

      async loadMemoryGraph(agentName = null) {
        this.memoryGraphLoading = true;
        this.memoryGraphError = "";
        this.memoryGraphSelected = null;
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
          onSelect: nodeId => this.selectMemoryGraphNode(nodeId),
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
      },
    };
  }

  window.agentGalMemoryGraph = {
    createState,
    createNetwork,
    isReady,
  };
})();
