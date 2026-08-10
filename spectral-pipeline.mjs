import baseRuleset from "./.spectral.mjs";
import f5PathParams from "./spectral/functions/f5-path-params.js";

export default {
  extends: [baseRuleset],
  rules: {
    "f5-path-params": {
      description: "Path parameters must be declared and used (supports dotted names like metadata.namespace)",
      message: "{{error}}",
      severity: "error",
      given: "$.paths[*]",
      then: {
        function: f5PathParams,
      }
    }
  }
};
