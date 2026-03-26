import fs from 'node:fs'
import path from 'node:path'

import { getWorkspaceRoot } from './fixture-batch.js'

export function getSemanticBaselinePath() {
  return process.env.ASR_E2E_BASELINE_PATH || path.join(
    getWorkspaceRoot(),
    '6-skills',
    'funasr-task-manager-web-e2e',
    'references',
    'semantic-baseline.json',
  )
}

export function loadSemanticBaseline() {
  const baselinePath = getSemanticBaselinePath()
  if (!fs.existsSync(baselinePath)) {
    return {
      baselinePath,
      exists: false,
      files: {},
      version: 1,
    }
  }

  try {
    const payload = JSON.parse(fs.readFileSync(baselinePath, 'utf-8'))
    return {
      baselinePath,
      exists: true,
      files: payload.files || {},
      version: payload.version || 1,
    }
  } catch (error) {
    throw new Error(`Semantic baseline is invalid JSON: ${baselinePath}. ${error.message}`)
  }
}

export function evaluateTranscriptAgainstBaseline(fileName, text, semanticBaseline) {
  const baseline = semanticBaseline.files[fileName]
  if (!baseline) {
    return {
      status: 'not-configured',
      passed: true,
    }
  }

  const keywordsAll = Array.isArray(baseline.keywords_all) ? baseline.keywords_all : []
  const keywordsAny = Array.isArray(baseline.keywords_any) ? baseline.keywords_any : []
  const missingAll = keywordsAll.filter((keyword) => !text.includes(keyword))
  const matchedAny = keywordsAny.filter((keyword) => text.includes(keyword))
  const expectedLanguage = baseline.expected_language || null
  const languagePassed = expectedLanguage !== 'zh-CN' || /[\u4e00-\u9fff]/.test(text)
  const anyPassed = keywordsAny.length === 0 || matchedAny.length > 0
  const passed = missingAll.length === 0 && anyPassed && languagePassed

  return {
    status: 'configured',
    passed,
    expected_language: expectedLanguage,
    keywords_all: keywordsAll,
    keywords_any: keywordsAny,
    missing_keywords_all: missingAll,
    matched_keywords_any: matchedAny,
    language_passed: languagePassed,
    notes: baseline.notes || '',
  }
}