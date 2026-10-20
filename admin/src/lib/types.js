/**
 * @typedef {'skill' | 'grounding'} Kind
 * @typedef {'draft' | 'published' | 'unpublished'} EntryStatus
 * @typedef {'draft' | 'published' | 'deprecated'} VersionStatus
 * @typedef {'read' | 'write'} Scope
 */

/**
 * @typedef {Object} EntrySummary
 * @property {string} id
 * @property {string} name
 * @property {string} display_name
 * @property {string} description
 * @property {Kind} kind
 * @property {string | null} category
 * @property {string[]} tags
 * @property {EntryStatus} status
 * @property {string | null} latest_version
 * @property {string} owner_id
 * @property {string} created_at
 * @property {string} updated_at
 */

/**
 * @typedef {Object} VersionSummary
 * @property {string} id
 * @property {string} skill_id
 * @property {string} version
 * @property {VersionStatus} status
 * @property {string} sha256
 * @property {number} size_bytes
 * @property {number} uncompressed_bytes
 * @property {number} file_count
 * @property {string} entrypoint
 * @property {string} display_name
 * @property {string} description
 * @property {string | null} category
 * @property {string[]} tags
 * @property {string} uploaded_by
 * @property {string | null} published_by
 * @property {string | null} published_at
 * @property {string | null} deprecated_at
 * @property {string} created_at
 */

/**
 * @typedef {VersionSummary & { metadata: Record<string, unknown>, download_url: string }} VersionDetail
 * @typedef {EntrySummary & { versions: VersionSummary[] }} EntryDetail
 */

/**
 * @typedef {Object} ManifestEntry
 * @property {string} path
 * @property {number} size
 * @property {string} sha256
 * @property {string} media_type
 * @property {boolean} text
 */

/**
 * @typedef {Object} FileListing
 * @property {string} name
 * @property {string} version
 * @property {ManifestEntry[]} files
 */

/**
 * @typedef {Object} FileContent
 * @property {string} path
 * @property {number} size
 * @property {string} sha256
 * @property {string} media_type
 * @property {boolean} text
 * @property {string | null} content
 * @property {boolean} truncated
 * @property {string} encoding
 */

/**
 * @typedef {Object} Page
 * @property {EntrySummary[]} items
 * @property {number} total
 * @property {number} limit
 * @property {number} offset
 */

/**
 * @typedef {Object} TaxonomyItem
 * @property {string} name
 * @property {number} count
 */

/**
 * @typedef {Object} Limits
 * @property {number} max_package_bytes
 * @property {number} max_uncompressed_bytes
 * @property {number} max_file_bytes
 * @property {number} max_files
 * @property {number} max_inline_read_bytes
 */

/**
 * @typedef {Object} RegistryInfo
 * @property {string} name
 * @property {string} version
 * @property {boolean} public_read
 * @property {string} public_url
 * @property {string} mcp_path
 * @property {Limits} limits
 */

/**
 * @typedef {Object} User
 * @property {string} id
 * @property {string} username
 * @property {string} email
 * @property {string} role
 * @property {boolean} is_active
 * @property {string} created_at
 */

/**
 * @typedef {Object} ApiToken
 * @property {string} id
 * @property {string} name
 * @property {string} prefix
 * @property {Scope} scope
 * @property {string} created_at
 * @property {string | null} expires_at
 * @property {string | null} last_used_at
 * @property {string | null} revoked_at
 */

/**
 * @typedef {ApiToken & { token: string }} CreatedToken
 * @typedef {{ entry: EntrySummary, version: VersionSummary }} UploadResult
 */

export {}
