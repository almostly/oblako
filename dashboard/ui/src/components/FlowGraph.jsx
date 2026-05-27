import { ReactFlow, Background, Controls } from '@xyflow/react'
import '@xyflow/react/dist/style.css'

// Node accent by ASL state type; status overrides the border + fill when a run is live.
const TYPE_COLOR = {
  Task: '#0972d3', Choice: '#ff9900', Pass: '#5f6b7a', Wait: '#5f6b7a',
  Succeed: '#037f0c', Fail: '#d91515', Map: '#8b5cf6', Parallel: '#8b5cf6',
}
const STATUS = {
  SUCCEEDED: { border: '#037f0c', bg: '#f0fbf1' },
  FAILED: { border: '#d91515', bg: '#fff5f5' },
  RUNNING: { border: '#0972d3', bg: '#f1f8ff' },
}

function buildGraph(definition, statusByState) {
  const states = definition.States || {}
  const depth = {}
  const order = []
  const seen = new Set()
  const queue = [[definition.StartAt, 0]]
  while (queue.length) {
    const [name, d] = queue.shift()
    if (!name || seen.has(name) || !states[name]) continue
    seen.add(name); depth[name] = d; order.push(name)
    const s = states[name]
    const nexts = []
    if (s.Next) nexts.push(s.Next)
    if (s.Default) nexts.push(s.Default)
    for (const c of s.Choices || []) if (c.Next) nexts.push(c.Next)
    for (const n of nexts) queue.push([n, d + 1])
  }
  const maxDepth = order.length ? Math.max(...Object.values(depth)) : 0
  for (const name of Object.keys(states)) {
    if (!(name in depth)) { depth[name] = maxDepth + 1; order.push(name) }
  }
  const perDepth = {}
  const nodes = order.map((name) => {
    const d = depth[name]
    const idx = perDepth[d] || 0
    perDepth[d] = idx + 1
    const s = states[name]
    const st = statusByState[name]
    const accent = TYPE_COLOR[s.Type] || '#5f6b7a'
    return {
      id: name,
      data: { label: `${name}\n${s.Type}` },
      position: { x: 40 + idx * 240, y: 30 + d * 110 },
      sourcePosition: 'bottom',
      targetPosition: 'top',
      style: {
        border: `2px solid ${st && STATUS[st] ? STATUS[st].border : accent}`,
        borderLeft: `6px solid ${accent}`,
        borderRadius: 8, padding: '8px 12px', fontSize: 12, width: 200,
        whiteSpace: 'pre-line', textAlign: 'left',
        background: st && STATUS[st] ? STATUS[st].bg : '#fff',
        color: '#16191f', fontWeight: 600,
      },
    }
  })
  const edges = []
  const add = (from, to, label) => {
    if (!states[to]) return
    edges.push({
      id: `${from}->${to}-${label || ''}`, source: from, target: to, label,
      animated: statusByState[from] === 'RUNNING',
      style: { stroke: '#879596' }, labelStyle: { fontSize: 11, fill: '#5f6b7a' },
      markerEnd: { type: 'arrowclosed', color: '#879596' },
    })
  }
  for (const [name, s] of Object.entries(states)) {
    if (s.Next) add(name, s.Next)
    if (s.Default) add(name, s.Default, 'default')
    for (const c of s.Choices || []) add(name, c.Next, 'choice')
  }
  return { nodes, edges }
}

export default function FlowGraph({ definition, statusByState = {}, height = 380 }) {
  const { nodes, edges } = buildGraph(definition || {}, statusByState)
  return (
    <div style={{ height, border: '1px solid #e9ebed', borderRadius: 8, background: '#fafbfc' }}>
      <ReactFlow
        nodes={nodes} edges={edges} fitView
        nodesDraggable={false} nodesConnectable={false} elementsSelectable={false}
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={16} color="#e9ebed" />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  )
}
