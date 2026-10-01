"""Catch package-origin failures without matching their human-readable messages.

Each private concrete class combines one observed builtin exception/category
with the public package/domain family. Builtin constructors, args and catches
remain intact. No builtin is shadowed at emission sites, and exceptions from
imports, dependencies and injected callbacks are never recategorized.
"""


class MetasalmonCondition(Exception):
    """Base for conditions explicitly emitted by metasalmonpy."""


class MetasalmonError(MetasalmonCondition):
    """Base for package-origin errors; excludes dependency exceptions."""


class MetasalmonWarning(Warning, MetasalmonCondition):
    """Base for package-origin warnings, preserving original warning filters."""


class MetasalmonValidationError(MetasalmonError):
    """Package-origin validation failure."""


class MetasalmonValidationWarning(MetasalmonWarning):
    """Package-origin validation warning."""


class MetasalmonLlmError(MetasalmonError):
    """Package-origin llm failure."""


class MetasalmonLlmWarning(MetasalmonWarning):
    """Package-origin llm warning."""


class MetasalmonPublicationError(MetasalmonError):
    """Package-origin publication failure."""


class MetasalmonPublicationWarning(MetasalmonWarning):
    """Package-origin publication warning."""


class MetasalmonRetrievalError(MetasalmonError):
    """Package-origin retrieval failure."""


class MetasalmonRetrievalWarning(MetasalmonWarning):
    """Package-origin retrieval warning."""


# These explicit subclasses preserve the builtin type at each existing site.
# Keep constructors inherited: OSError attributes and KeyError rendering matter.


class _LlmFileNotFoundError(FileNotFoundError, MetasalmonLlmError):
    """Internal FileNotFoundError with the MetasalmonLlmError catch surface."""


class _LlmTypeError(TypeError, MetasalmonLlmError):
    """Internal TypeError with the MetasalmonLlmError catch surface."""


class _LlmUserWarning(UserWarning, MetasalmonLlmWarning):
    """Internal UserWarning with the MetasalmonLlmWarning catch surface."""


class _LlmValueError(ValueError, MetasalmonLlmError):
    """Internal ValueError with the MetasalmonLlmError catch surface."""


class _PackageAttributeError(AttributeError, MetasalmonError):
    """Internal AttributeError with the MetasalmonError catch surface."""


class _PackageDeprecationWarning(DeprecationWarning, MetasalmonWarning):
    """Internal DeprecationWarning with the MetasalmonWarning catch surface."""


class _PackageFileExistsError(FileExistsError, MetasalmonError):
    """Internal FileExistsError with the MetasalmonError catch surface."""


class _PackageFileNotFoundError(FileNotFoundError, MetasalmonError):
    """Internal FileNotFoundError with the MetasalmonError catch surface."""


class _PackageKeyError(KeyError, MetasalmonError):
    """Internal KeyError with the MetasalmonError catch surface."""


class _PackageNotADirectoryError(NotADirectoryError, MetasalmonError):
    """Internal NotADirectoryError with the MetasalmonError catch surface."""


class _PackageTypeError(TypeError, MetasalmonError):
    """Internal TypeError with the MetasalmonError catch surface."""


class _PackageUserWarning(UserWarning, MetasalmonWarning):
    """Internal UserWarning with the MetasalmonWarning catch surface."""


class _PackageValueError(ValueError, MetasalmonError):
    """Internal ValueError with the MetasalmonError catch surface."""


class _PublicationFileExistsError(FileExistsError, MetasalmonPublicationError):
    """Internal FileExistsError with the MetasalmonPublicationError catch surface."""


class _PublicationFileNotFoundError(FileNotFoundError, MetasalmonPublicationError):
    """Internal FileNotFoundError with the MetasalmonPublicationError catch surface."""


class _PublicationPermissionError(PermissionError, MetasalmonPublicationError):
    """Internal PermissionError with the MetasalmonPublicationError catch surface."""


class _PublicationRuntimeError(RuntimeError, MetasalmonPublicationError):
    """Internal RuntimeError with the MetasalmonPublicationError catch surface."""


class _PublicationTypeError(TypeError, MetasalmonPublicationError):
    """Internal TypeError with the MetasalmonPublicationError catch surface."""


class _PublicationUserWarning(UserWarning, MetasalmonPublicationWarning):
    """Internal UserWarning with the MetasalmonPublicationWarning catch surface."""


class _PublicationValueError(ValueError, MetasalmonPublicationError):
    """Internal ValueError with the MetasalmonPublicationError catch surface."""


class _RetrievalDeprecationWarning(DeprecationWarning, MetasalmonRetrievalWarning):
    """Internal DeprecationWarning with the MetasalmonRetrievalWarning catch surface."""


class _RetrievalRuntimeError(RuntimeError, MetasalmonRetrievalError):
    """Internal RuntimeError with the MetasalmonRetrievalError catch surface."""


class _RetrievalRuntimeWarning(RuntimeWarning, MetasalmonRetrievalWarning):
    """Internal RuntimeWarning with the MetasalmonRetrievalWarning catch surface."""


class _RetrievalValueError(ValueError, MetasalmonRetrievalError):
    """Internal ValueError with the MetasalmonRetrievalError catch surface."""


class _ValidationFileExistsError(FileExistsError, MetasalmonValidationError):
    """Internal FileExistsError with the MetasalmonValidationError catch surface."""


class _ValidationFileNotFoundError(FileNotFoundError, MetasalmonValidationError):
    """Internal FileNotFoundError with the MetasalmonValidationError catch surface."""


class _ValidationKeyError(KeyError, MetasalmonValidationError):
    """Internal KeyError with the MetasalmonValidationError catch surface."""


class _ValidationNotImplementedError(NotImplementedError, MetasalmonValidationError):
    """Internal NotImplementedError with the MetasalmonValidationError catch surface."""


class _ValidationRuntimeError(RuntimeError, MetasalmonValidationError):
    """Internal RuntimeError with the MetasalmonValidationError catch surface."""


class _ValidationRuntimeWarning(RuntimeWarning, MetasalmonValidationWarning):
    """Internal RuntimeWarning with the MetasalmonValidationWarning catch surface."""


class _ValidationTypeError(TypeError, MetasalmonValidationError):
    """Internal TypeError with the MetasalmonValidationError catch surface."""


class _ValidationUserWarning(UserWarning, MetasalmonValidationWarning):
    """Internal UserWarning with the MetasalmonValidationWarning catch surface."""


class _ValidationValueError(ValueError, MetasalmonValidationError):
    """Internal ValueError with the MetasalmonValidationError catch surface."""


__all__ = ['MetasalmonCondition', 'MetasalmonError', 'MetasalmonWarning', 'MetasalmonValidationError', 'MetasalmonValidationWarning', 'MetasalmonLlmError', 'MetasalmonLlmWarning', 'MetasalmonPublicationError', 'MetasalmonPublicationWarning', 'MetasalmonRetrievalError', 'MetasalmonRetrievalWarning']
