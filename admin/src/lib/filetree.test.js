import { describe, expect, it } from 'vitest'
import {
  ancestorsOf,
  buildTree,
  findNode,
  flattenTree,
  initialExpanded,
} from './filetree.js'

function entry(path, size = 10, text = true) {
  return { path, size, sha256: 'f'.repeat(64), media_type: 'text/plain', text }
}

const MANIFEST = [
  entry('SKILL.md', 100),
  entry('scripts/run.py', 30),
  entry('scripts/helpers/util.py', 20),
  entry('references/api.md', 50),
  entry('references/Zeta.md', 5),
  entry('data.sqlite', 1000, false),
]

describe('buildTree', () => {
  it('nests files under their directories', () => {
    const root = buildTree(MANIFEST)
    expect(root.type).toBe('dir')
    expect(root.children.map((node) => node.name)).toEqual([
      'references',
      'scripts',
      'data.sqlite',
      'SKILL.md',
    ])
    const scripts = findNode(root, 'scripts')
    expect(scripts?.type).toBe('dir')
    expect(findNode(root, 'scripts/helpers/util.py')?.type).toBe('file')
  })

  it('lists directories first and sorts names without regard to case', () => {
    const root = buildTree([entry('b.txt'), entry('A.txt'), entry('c/x.txt'), entry('Z/y.txt')])
    expect(root.children.map((node) => node.name)).toEqual(['c', 'Z', 'A.txt', 'b.txt'])
  })

  it('aggregates sizes and file counts per directory', () => {
    const root = buildTree(MANIFEST)
    expect(root.fileCount).toBe(6)
    expect(root.size).toBe(100 + 30 + 20 + 50 + 5 + 1000)
    const scripts = findNode(root, 'scripts')
    expect(scripts && scripts.type === 'dir' && scripts.fileCount).toBe(2)
    expect(scripts?.size).toBe(50)
  })

  it('keeps the manifest entry on every file node', () => {
    const root = buildTree(MANIFEST)
    const node = findNode(root, 'data.sqlite')
    expect(node && node.type === 'file' && node.entry.text).toBe(false)
  })

  it('handles an empty manifest and ignores empty paths', () => {
    expect(buildTree([]).children).toEqual([])
    expect(buildTree([entry('')]).children).toEqual([])
  })

  it('does not break on a path that is both a file and a directory', () => {
    const root = buildTree([entry('a/b.txt'), entry('a')])
    expect(root.fileCount).toBe(1)
  })

  it('treats names as text, never as markup', () => {
    const root = buildTree([entry('<img src=x onerror=alert(1)>.md')])
    expect(root.children[0].name).toBe('<img src=x onerror=alert(1)>.md')
  })
})

describe('ancestorsOf and findNode', () => {
  it('lists the directories that contain a path', () => {
    expect(ancestorsOf('a/b/c.txt')).toEqual(['a', 'a/b'])
    expect(ancestorsOf('c.txt')).toEqual([])
  })

  it('finds nodes and returns null for missing or invalid paths', () => {
    const root = buildTree(MANIFEST)
    expect(findNode(root, '')).toBe(root)
    expect(findNode(root, 'nope')).toBeNull()
    expect(findNode(root, 'SKILL.md/inner')).toBeNull()
  })
})

describe('initialExpanded', () => {
  it('opens everything in small packages', () => {
    const root = buildTree(MANIFEST)
    expect([...initialExpanded(root)].sort()).toEqual([
      'references',
      'scripts',
      'scripts/helpers',
    ])
  })

  it('opens only the first level and the path to the selection in big packages', () => {
    const many = Array.from({ length: 80 }, (_, index) => entry(`dir/sub${index % 4}/file${index}.txt`))
    const root = buildTree(many)
    const collapsed = initialExpanded(root, null)
    expect([...collapsed]).toEqual(['dir'])
    const opened = initialExpanded(root, 'dir/sub2/file2.txt')
    expect(opened.has('dir')).toBe(true)
    expect(opened.has('dir/sub2')).toBe(true)
    expect(opened.has('dir/sub1')).toBe(false)
  })
})

describe('flattenTree', () => {
  it('returns only the visible rows with their ARIA metadata', () => {
    const root = buildTree(MANIFEST)
    const rows = flattenTree(root, new Set(['scripts']))
    expect(rows.map((row) => row.node.path)).toEqual([
      'references',
      'scripts',
      'scripts/helpers',
      'scripts/run.py',
      'data.sqlite',
      'SKILL.md',
    ])
    const helpers = rows.find((row) => row.node.path === 'scripts/helpers')
    expect(helpers).toMatchObject({ depth: 1, expanded: false, setSize: 2, position: 1 })
    const scripts = rows.find((row) => row.node.path === 'scripts')
    expect(scripts).toMatchObject({ depth: 0, expanded: true, setSize: 4, position: 2 })
    expect(rows.find((row) => row.node.path === 'scripts/run.py')?.parentPath).toBe('scripts')
  })

  it('hides the children of collapsed directories', () => {
    const root = buildTree(MANIFEST)
    expect(flattenTree(root, new Set()).map((row) => row.node.path)).toEqual([
      'references',
      'scripts',
      'data.sqlite',
      'SKILL.md',
    ])
  })
})
