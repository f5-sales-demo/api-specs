import { oas } from "@stoplight/spectral-rulesets";
import { pattern } from "@stoplight/spectral-functions";

export default {
  extends: [oas],
  rules: {
    "path-params": "off",
    "oas3-valid-media-example": "warn",
    "operation-tags": "warn",
    "oas3-api-servers": "warn",
    "info-contact": "warn",
    "oas3-unused-component": "warn",
    "operation-operationId-unique": "error",
    "oas3-valid-schema-example": "error",
    "no-script-tags-in-markdown": "warn",
    "operation-description": "info",
    "info-description": "info",
    "sample-resource-example-naming": {
      description: "Sample resource example values should use the example- prefix",
      message: "{{value}} uses a non-standard placeholder prefix; use 'example-' for sample resource names",
      severity: "warn",
      given: ["$..example", "$..examples[*]"],
      then: {
        function: pattern,
        functionOptions: { notMatch: "^(my|test|foo|demo|sample)-[a-z]" }
      }
    }
  }
};
