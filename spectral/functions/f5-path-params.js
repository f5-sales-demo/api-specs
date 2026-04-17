const PARAM_PATTERN = /\{([^}]+)\}/g;

export default function (pathItem, _options, context) {
  const results = [];
  const pathString = context.path[1];

  if (typeof pathString !== 'string') {
    return results;
  }

  const placeholders = new Set();
  for (const match of pathString.matchAll(PARAM_PATTERN)) {
    placeholders.add(match[1]);
  }

  const httpMethods = ['get', 'put', 'post', 'delete', 'options', 'head', 'patch', 'trace'];

  for (const method of httpMethods) {
    const operation = pathItem[method];
    if (operation === undefined || operation === null) continue;

    const pathLevelParams = Array.isArray(pathItem.parameters) ? pathItem.parameters : [];
    const opLevelParams = Array.isArray(operation.parameters) ? operation.parameters : [];

    const declaredPathParams = new Set();
    for (const param of [...pathLevelParams, ...opLevelParams]) {
      if (param && param.in === 'path' && param.name) {
        declaredPathParams.add(param.name);
      }
    }

    for (const placeholder of placeholders) {
      if (declaredPathParams.has(placeholder) === false) {
        results.push({
          message: `Operation "${method}" on path "${pathString}" uses path parameter "{${placeholder}}" but it is not declared in parameters.`,
          path: [...context.path, method],
        });
      }
    }

    for (const paramName of declaredPathParams) {
      if (placeholders.has(paramName) === false) {
        results.push({
          message: `Operation "${method}" on path "${pathString}" declares path parameter "${paramName}" but it is not used in the path.`,
          path: [...context.path, method],
        });
      }
    }
  }

  return results;
}
