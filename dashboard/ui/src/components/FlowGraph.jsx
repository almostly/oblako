import { ReactFlow, Background, Controls, MarkerType } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import dagre from '@dagrejs/dagre'

// AWS Workflow Studio-ish: white cards with a type-coloured left accent, Start/End
// pills, dagre top-down layout, and orthogonal (smoothstep) connectors.
const TYPE_COLOR = {
  Task: '#0972d3', Choice: '#ff9900', Pass: '#5f6b7a', Wait: '#5f6b7a',
  Succeed: '#037f0c', Fail: '#d91515', Map: '#8b5cf6', Parallel: '#8b5cf6',
}
const STATUS = {
  SUCCEEDED: { border: '#037f0c', bg: '#f0fbf1' },
  FAILED: { border: '#d91515', bg: '#fff5f5' },
  RUNNING: { border: '#0972d3', bg: '#f1f8ff' },
}
const CARD = { w: 230, h: 60 }
const PILL = { w: 88, h: 38 }

function pillNode(id, label, bg) {
  return {
    id, data: { label }, __w: PILL.w, __h: PILL.h,
    sourcePosition: 'bottom', targetPosition: 'top',
    style: {
      width: PILL.w, height: PILL.h, borderRadius: 19, border: 'none',
      background: bg, color: '#fff', fontSize: 12, fontWeight: 700,
      display: 'flex', alignItems: 'center', justifyContent: 'center',
    },
  }
}

function buildGraph(definition, statusByState) {
  const states = definition.States || {}
  const nodes = [pillNode('__start__', 'Start', '#16191f')]
  for (const [name, s] of Object.entries(states)) {
    const st = statusByState[name]
    const accent = TYPE_COLOR[s.Type] || '#5f6b7a'
    nodes.push({
      id: name, data: { label: `${name}\n${s.Type}` }, __w: CARD.w, __h: CARD.h,
      sourcePosition: 'bottom', targetPosition: 'top',
      style: {
        width: CARD.w, height: CARD.h, borderRadius: 8,
        border: `1px solid ${st && STATUS[st] ? STATUS[st].border : '#c6c6cd'}`,
        borderLeft: `5px solid ${accent}`,
        background: st && STATUS[st] ? STATUS[st].bg : '#ffffff',
        color: '#16191f', fontSize: 12, fontWeight: 600,
        display: 'flex', flexDirection: 'column', justifyContent: 'center',
        padding: '0 12px', whiteSpace: 'pre-line', textAlign: 'left',
        boxShadow: '0 1px 3px rgba(0,0,0,0.10)',
      },
    })
  }
  const hasEnd = Object.values(states).some(s => s.End || s.Type === 'Succeed' || s.Type === 'Fail')
  if (hasEnd) nodes.push(pillNode('__end__', 'End', '#5f6b7a'))

  const edges = []
  const edge = (from, to, label) => {
    if (!to || (to !== '__end__' && !states[to])) return
    edges.push({
      id: `${from}->${to}-${label || ''}`, source: from, target: to, label, type: 'smoothstep',
      animated: statusByState[from] === 'RUNNING',
      style: { stroke: '#879596', strokeWidth: 1.5 },
      labelStyle: { fontSize: 11, fill: '#5f6b7a' }, labelBgPadding: [4, 2],
      markerEnd: { type: MarkerType.ArrowClosed, color: '#879596', width: 16, height: 16 },
    })
  }
  edge('__start__', definition.StartAt)
  for (const [name, s] of Object.entries(states)) {
    if (s.Next) edge(name, s.Next)
    if (s.Default) edge(name, s.Default, 'default')
    for (const c of s.Choices || []) edge(name, c.Next, 'choice')
    if (s.End || s.Type === 'Succeed' || s.Type === 'Fail') edge(name, '__end__')
  }

  // dagre top-down layout; convert centre coords to React Flow's top-left origin.
  const g = new dagre.graphlib.Graph()
  g.setGraph({ rankdir: 'TB', nodesep: 50, ranksep: 64, marginx: 16, marginy: 16 })
  g.setDefaultEdgeLabel(() => ({}))
  nodes.forEach(n => g.setNode(n.id, { width: n.__w, height: n.__h }))
  edges.forEach(e => g.setEdge(e.source, e.target))
  dagre.layout(g)
  nodes.forEach(n => {
    const p = g.node(n.id)
    n.position = { x: p.x - n.__w / 2, y: p.y - n.__h / 2 }
    delete n.__w; delete n.__h
  })
  return { nodes, edges }
}

export default function FlowGraph({ definition, statusByState = {}, height = 420 }) {
  const { nodes, edges } = buildGraph(definition || {}, statusByState)
  return (
    <div style={{ height, border: '1px solid #e9ebed', borderRadius: 8, background: '#fafbfc' }}>
      <ReactFlow
        nodes={nodes} edges={edges} fitView fitViewOptions={{ padding: 0.2 }}
        nodesDraggable={false} nodesConnectable={false} elementsSelectable={false}
        proOptions={{ hideAttribution: true }} minZoom={0.2}
      >
        <Background gap={16} color="#e9ebed" />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  )
}
