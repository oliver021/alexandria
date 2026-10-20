/**
 * @typedef {Object} FileNode
 * @property {'file'} type
 * @property {string} name
 * @property {string} path
 * @property {number} size
 * @property {import('./types.js').ManifestEntry} entry
 */

/**
 * @typedef {Object} DirNode
 * @property {'dir'} type
 * @property {string} name
 * @property {string} path
 * @property {number} size
 * @property {number} fileCount
 * @property {TreeNode[]} children
 */

/**
 * @typedef {FileNode | DirNode} TreeNode
 */

/**
 * @typedef {Object} TreeRow
 * @property {TreeNode} node
 * @property {number} depth
 * @property {boolean} expanded
 * @property {number} setSize
 * @property {number} position
 * @property {string} parentPath
 */

/**
 * @param {string} a
 * @param {string} b
 * @returns {number}
 */
function compareNames(a, b) {
  const lowerA = a.toLowerCase()
  const lowerB = b.toLowerCase()
  if (lowerA < lowerB) return -1
  if (lowerA > lowerB) return 1
  if (a < b) return -1
  if (a > b) return 1
  return 0
}

/**
 * @param {TreeNode} a
 * @param {TreeNode} b
 * @returns {number}
 */
function compareNodes(a, b) {
  if (a.type !== b.type) return a.type === 'dir' ? -1 : 1
  return compareNames(a.name, b.name)
}

/**
 * @param {DirNode} dir
 */
function finalize(dir) {
  dir.children.sort(compareNodes)
  let size = 0
  let fileCount = 0
  for (const child of dir.children) {
    if (child.type === 'dir') {
      finalize(child)
      size += child.size
      fileCount += child.fileCount
    } else {
      size += child.size
      fileCount += 1
    }
  }
  dir.size = size
  dir.fileCount = fileCount
}

/**
 * @param {import('./types.js').ManifestEntry[]} entries
 * @returns {DirNode}
 */
export function buildTree(entries) {
  /** @type {DirNode} */
  const root = { type: 'dir', name: '', path: '', size: 0, fileCount: 0, children: [] }
  /** @type {Map<string, DirNode>} */
  const directories = new Map([['', root]])

  for (const entry of entries) {
    const segments = entry.path.split('/').filter((segment) => segment !== '')
    if (segments.length === 0) continue
    let parent = root
    for (let index = 0; index < segments.length - 1; index += 1) {
      const path = segments.slice(0, index + 1).join('/')
      let dir = directories.get(path)
      if (!dir) {
        dir = { type: 'dir', name: segments[index], path, size: 0, fileCount: 0, children: [] }
        directories.set(path, dir)
        parent.children.push(dir)
      }
      parent = dir
    }
    const name = segments[segments.length - 1]
    const path = segments.join('/')
    if (directories.has(path)) continue
    parent.children.push({ type: 'file', name, path, size: entry.size, entry })
  }

  finalize(root)
  return root
}

/**
 * @param {string} path
 * @returns {string[]}
 */
export function ancestorsOf(path) {
  const segments = path.split('/').filter((segment) => segment !== '')
  const result = []
  for (let index = 1; index < segments.length; index += 1) {
    result.push(segments.slice(0, index).join('/'))
  }
  return result
}

/**
 * @param {DirNode} root
 * @param {string} path
 * @returns {TreeNode | null}
 */
export function findNode(root, path) {
  if (path === '') return root
  let current = root
  const segments = path.split('/')
  for (let index = 0; index < segments.length; index += 1) {
    const child = current.children.find((node) => node.name === segments[index])
    if (!child) return null
    if (index === segments.length - 1) return child
    if (child.type !== 'dir') return null
    current = child
  }
  return null
}

/**
 * @param {DirNode} root
 * @param {string | null} [selectedPath]
 * @param {number} [expandAllLimit]
 * @returns {Set<string>}
 */
export function initialExpanded(root, selectedPath = null, expandAllLimit = 60) {
  /** @type {Set<string>} */
  const expanded = new Set()
  /**
   * @param {DirNode} dir
   */
  const visit = (dir) => {
    for (const child of dir.children) {
      if (child.type === 'dir') {
        expanded.add(child.path)
        visit(child)
      }
    }
  }
  if (root.fileCount <= expandAllLimit) {
    visit(root)
    return expanded
  }
  for (const child of root.children) {
    if (child.type === 'dir') expanded.add(child.path)
  }
  if (selectedPath) {
    for (const path of ancestorsOf(selectedPath)) expanded.add(path)
  }
  return expanded
}

/**
 * @param {DirNode} root
 * @param {Set<string>} expanded
 * @returns {TreeRow[]}
 */
export function flattenTree(root, expanded) {
  /** @type {TreeRow[]} */
  const rows = []
  /**
   * @param {DirNode} dir
   * @param {number} depth
   */
  const visit = (dir, depth) => {
    dir.children.forEach((node, index) => {
      const isOpen = node.type === 'dir' && expanded.has(node.path)
      rows.push({
        node,
        depth,
        expanded: isOpen,
        setSize: dir.children.length,
        position: index + 1,
        parentPath: dir.path,
      })
      if (node.type === 'dir' && isOpen) visit(node, depth + 1)
    })
  }
  visit(root, 0)
  return rows
}
