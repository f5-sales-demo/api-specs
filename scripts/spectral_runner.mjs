import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import corePkg from '@stoplight/spectral-core';
import parsersPkg from '@stoplight/spectral-parsers';

const { Spectral, Document } = corePkg;
const { Json } = parsersPkg;

async function main() {
  const args = process.argv.slice(2);
  let rulesetPath = null;
  const files = [];

  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--ruleset') {
      rulesetPath = args[i + 1];
      i++;
    } else {
      files.push(args[i]);
    }
  }

  if (!rulesetPath) {
    console.error('Error: --ruleset parameter is required');
    process.exit(2);
  }

  if (files.length === 0) {
    console.error('Error: At least one specification file must be specified');
    process.exit(2);
  }

  const resolvedRulesetPath = path.resolve(rulesetPath);
  const fileUrlPath = pathToFileURL(resolvedRulesetPath).href;
  let ruleset;
  try {
    const rulesetModule = await import(fileUrlPath);
    ruleset = rulesetModule.default;
  } catch (err) {
    console.error(`Error loading ruleset from ${fileUrlPath}:`, err);
    process.exit(2);
  }

  const spectral = new Spectral();
  try {
    spectral.setRuleset(ruleset);
  } catch (err) {
    console.error('Error setting Spectral ruleset:', err);
    process.exit(2);
  }

  const allFindings = [];

  for (const file of files) {
    const resolvedFilePath = path.resolve(file);
    if (!fs.existsSync(resolvedFilePath)) {
      console.error(`Error: File does not exist: ${file}`);
      process.exit(2);
    }

    const content = fs.readFileSync(resolvedFilePath, 'utf8');
    try {
      const document = new Document(content, Json, file);
      const findings = await spectral.run(document);
      for (const finding of findings) {
        allFindings.push({
          code: finding.code,
          message: finding.message,
          path: finding.path,
          range: finding.range,
          severity: finding.severity,
          source: finding.source || file,
        });
      }
    } catch (err) {
      console.error(`Error running Spectral on ${file}:`, err);
      process.exit(2);
    }
  }

  console.log(JSON.stringify(allFindings, null, 2));
}

main().catch((err) => {
  console.error('Fatal error:', err);
  process.exit(2);
});
