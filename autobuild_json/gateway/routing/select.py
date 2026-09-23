def eligible(resolved_model, allowed_models, required, supported, healthy):
    return healthy and resolved_model in allowed_models and required <= supported
