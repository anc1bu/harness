// D3 force-directed relationship graph component.
// Usage: mountGraph(svgEl, { nodes, links, onNodeClick })
// Each node: { id, label, group }
// Each link: { source, target }

export function mountGraph(svgEl, { nodes, links, onNodeClick = () => {} }) {
  const svg    = d3.select(svgEl);
  const width  = svgEl.clientWidth;
  const height = svgEl.clientHeight;

  svg.selectAll('*').remove();

  const g = svg.append('g');

  // Zoom/pan
  svg.call(d3.zoom().scaleExtent([0.3, 3]).on('zoom', e => g.attr('transform', e.transform)));

  const simulation = d3.forceSimulation(nodes)
    .force('link',   d3.forceLink(links).id(d => d.id).distance(120))
    .force('charge', d3.forceManyBody().strength(-300))
    .force('center', d3.forceCenter(width / 2, height / 2))
    .force('collision', d3.forceCollide(40));

  const link = g.append('g')
    .selectAll('line')
    .data(links)
    .join('line')
    .attr('class', 'link');

  const node = g.append('g')
    .selectAll('g')
    .data(nodes)
    .join('g')
    .attr('class', 'node')
    .call(d3.drag()
      .on('start', (e, d) => { if (!e.active) simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
      .on('drag',  (e, d) => { d.fx = e.x; d.fy = e.y; })
      .on('end',   (e, d) => { if (!e.active) simulation.alphaTarget(0); d.fx = null; d.fy = null; })
    )
    .on('click', (e, d) => onNodeClick(d));

  node.append('circle')
    .attr('r', 18)
    .attr('fill', 'var(--panel)')
    .attr('stroke', 'var(--accent)');

  node.append('text')
    .attr('dy', 32)
    .attr('text-anchor', 'middle')
    .text(d => d.label || d.id);

  simulation.on('tick', () => {
    link
      .attr('x1', d => d.source.x).attr('y1', d => d.source.y)
      .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
    node.attr('transform', d => `translate(${d.x},${d.y})`);
  });

  return simulation;
}

export function highlightNode(svgEl, nodeId) {
  d3.select(svgEl).selectAll('.node').classed('highlighted', d => d.id === nodeId);
  d3.select(svgEl).selectAll('.link').classed('highlighted', d => d.source.id === nodeId || d.target.id === nodeId);
}
