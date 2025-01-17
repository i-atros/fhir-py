import json
import copy
import logging
import warnings
from abc import ABC, abstractmethod
from json import JSONDecodeError

import aiohttp
import requests
import urllib

from yarl import URL
from fhirpy.base.searchset import AbstractSearchSet
from fhirpy.base.resource import BaseResource, BaseReference
from fhirpy.base.utils import (
    AttrDict,
    encode_params,
    get_by_path,
    parse_pagination_url,
    remove_prefix,
)
from fhirpy.base.exceptions import (
    ResourceNotFound,
    OperationOutcome,
    InvalidResponse,
    MultipleResourcesFound,
)


class AbstractClient(ABC):
    url = None
    authorization = None
    extra_headers = None

    def __init__(self, url, authorization=None, extra_headers=None):
        self.url = url
        self.authorization = authorization
        self.extra_headers = extra_headers

    def __str__(self):  # pragma: no cover
        return f"<{self.__class__.__name__} {self.url}>"

    def __repr__(self):  # pragma: no cover
        return self.__str__()

    @property  # pragma: no cover
    @abstractmethod
    def searchset_class(self):
        pass

    @property  # pragma: no cover
    @abstractmethod
    def resource_class(self):
        pass

    @abstractmethod  # pragma: no cover
    def reference(self, resource_type=None, id=None, reference=None, **kwargs):
        pass

    def resource(self, resource_type=None, **kwargs):
        if resource_type is None:
            raise TypeError("Argument `resource_type` is required")

        return self.resource_class(self, resource_type=resource_type, **kwargs)

    def resources(self, resource_type):
        return self.searchset_class(self, resource_type=resource_type)

    @abstractmethod  # pragma: no cover
    def execute(self, path, method=None, **kwargs):
        pass

    @abstractmethod  # pragma: no cover
    def _do_request(self, method, path, data=None, params=None, returning_status=False):
        pass

    @abstractmethod  # pragma: no cover
    def _fetch_resource(self, path, params=None):
        pass

    def _build_request_headers(self):
        headers = {"Accept": "application/fhir+json"}

        if self.authorization:
            headers["Authorization"] = self.authorization

        if self.extra_headers is not None:
            headers = {**headers, **self.extra_headers}

        return headers

    def _build_request_url(self, path, params):
        if URL(path).is_absolute():
            if urllib.parse.urlparse(self.url).port:
                parsed = urllib.parse.urlparse(path)
                if parsed.port is None and parsed.scheme == "https":
                    path = f'{parsed.scheme}://{parsed.netloc}:443{parsed.path}?{parsed.query}'
                    if parsed.fragment != "":
                        path += f'#{parsed.fragment}'
            if self.url.rstrip("/") in path.rstrip("/"):
                return path
            raise ValueError(
                f'Request url "{path}" does not contain base url "{self.url}"'
                " (possible security issue)"
            )
        path = path.lstrip("/")
        base_url_path = URL(self.url).path.lstrip("/") + "/"
        path = remove_prefix(path, base_url_path)
        params = params or {}

        return f'{self.url.rstrip("/")}/{path.lstrip("/")}?{encode_params(params)}'


class AsyncClient(AbstractClient, ABC):
    aiohttp_config = None

    def __init__(self, url, authorization=None, extra_headers=None, aiohttp_config=None):
        self.aiohttp_config = aiohttp_config or {}

        super().__init__(url, authorization, extra_headers)

    async def execute(self, path, method="post", **kwargs):
        return await self._do_request(method, path, **kwargs)

    async def _do_request(self, method, path, data=None, params=None, returning_status=False):
        headers = self._build_request_headers()
        url = self._build_request_url(path, params)
        if method == 'patch':
            headers['Content-Type'] = 'application/json-patch+json'
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.request(method, url, json=data, **self.aiohttp_config) as r:
                if 200 <= r.status < 300:
                    data = await r.text()
                    r_data = json.loads(data, object_hook=AttrDict) if data else None
                    return (r_data, r.status) if returning_status else r_data

                if r.status == 404 or r.status == 410:
                    raise ResourceNotFound(await r.text())

                if r.status == 412:
                    raise MultipleResourcesFound(await r.text())

                data = await r.text()
                try:
                    parsed_data = json.loads(data)
                    if parsed_data["resourceType"] == "OperationOutcome":
                        raise OperationOutcome(resource=parsed_data)
                    raise OperationOutcome(reason=data)
                except (KeyError, JSONDecodeError):
                    raise OperationOutcome(reason=data)

    async def _fetch_resource(self, path, params=None):
        return await self._do_request("get", path, params=params)


class SyncClient(AbstractClient, ABC):
    requests_config = None

    def __init__(
            self, url, authorization=None, extra_headers=None, requests_config=None, timeout=120
    ):
        self.requests_config = requests_config or {}
        if 'timeout' not in self.requests_config:
            self.requests_config['timeout'] = timeout

        super().__init__(url, authorization, extra_headers)

    def execute(self, path, method="post", **kwargs):
        return self._do_request(method, path, **kwargs)

    def _do_request(self, method, path, data=None, params=None, returning_status=False):
        headers = self._build_request_headers()
        headers['Content-Type'] = 'application/fhir+json'
        if method == 'patch':
            headers['Content-Type'] = 'application/json-patch+json'
        url = self._build_request_url(path, params)
        if session := self.requests_config.pop('session', None):
            r = session.request(
                method, url, json=data, headers=headers, **self.requests_config,
            )
            # now add it again for the next request
            self.requests_config['session'] = session
        else:
            r = requests.request(
                method, url, json=data, headers=headers, **self.requests_config,
            )

        if 200 <= r.status_code < 300:
            r_data = json.loads(r.content.decode(), object_hook=AttrDict) if r.content else None
            return (r_data, r.status_code) if returning_status else r_data

        if r.status_code == 404 or r.status_code == 410:
            raise ResourceNotFound(r.content.decode())

        if r.status_code == 412:
            raise MultipleResourcesFound(r.content.decode())

        data = r.content.decode()
        try:
            parsed_data = json.loads(data)
            if parsed_data["resourceType"] == "OperationOutcome":
                raise OperationOutcome(resource=parsed_data)
            raise OperationOutcome(reason=data)
        except (KeyError, JSONDecodeError) as exc:
            raise OperationOutcome(reason=data) from exc

    def _fetch_resource(self, path, params=None):
        return self._do_request("get", path, params=params)


class SyncSearchSet(AbstractSearchSet, ABC):
    def fetch(self):
        bundle_data = self.client._fetch_resource(self.resource_type, self.params)
        resources = self._get_bundle_resources(bundle_data)
        return resources

    def fetch_raw(self):
        data = self.client._fetch_resource(self.resource_type, self.params)
        data_resource_type = data.get("resourceType", None)

        if data_resource_type == "Bundle":
            for item in data["entry"]:
                item.resource = self._dict_to_resource(item.resource)

        return data

    def fetch_all(self):
        return list(x for x in self)

    def get(self, id=None):
        searchset = self.limit(2)
        if id:
            warnings.warn(
                "parameter 'id' of method get() is deprecated "
                "and will be removed in future versions. "
                "Please use 'search(id='...').get()'",
                DeprecationWarning,
                stacklevel=2,
            )
            searchset = searchset.search(_id=id)
        res_data = searchset.fetch()
        if len(res_data) == 0:
            raise ResourceNotFound("No resources found")
        if len(res_data) > 1:
            raise MultipleResourcesFound("More than one resource found")
        resource = res_data[0]
        return self._dict_to_resource(resource)

    def count(self):
        new_params = copy.deepcopy(self.params)
        new_params["_count"] = 0
        new_params["_totalMethod"] = "count"

        return self.client._fetch_resource(self.resource_type, params=new_params)["total"]

    def first(self):
        result = self.limit(1).fetch()

        return result[0] if result else None

    def get_or_create(self, resource):
        assert resource.resource_type == self.resource_type
        data, status_code = self.client._do_request(
            "POST", self.resource_type, resource.serialize(), self.params, True
        )
        return data, (True if status_code == 201 else False)

    def update(self, resource):
        # TODO: Support cases where resource with id is provided
        # accordingly to the https://build.fhir.org/http.html#cond-update
        assert resource.resource_type == self.resource_type
        data, status_code = self.client._do_request(
            "PUT", self.resource_type, resource.serialize(), self.params, True
        )
        return data, (True if status_code == 201 else False)

    def patch(self, resource):
        # TODO: Handle cases where resource with id is provided
        assert resource.resource_type == self.resource_type
        # TODO: Should we omit resourceType after serialization? (not to pollute history)
        return self.client._do_request(
            "PATCH", self.resource_type, resource.serialize(), self.params
        )

    def delete(self):
        return self.client._do_request(
            "DELETE", self.resource_type, params=self.params, returning_status=True
        )

    def __iter__(self):
        next_link = None
        while True:
            if next_link:
                bundle_data = self.client._fetch_resource(*parse_pagination_url(next_link))
            else:
                bundle_data = self.client._fetch_resource(self.resource_type, self.params)
            new_resources = self._get_bundle_resources(bundle_data)
            next_link = get_by_path(bundle_data, ["link", {"relation": "next"}, "url"])

            for item in new_resources:
                yield item

            if not next_link:
                break


class AsyncSearchSet(AbstractSearchSet, ABC):
    async def fetch(self):
        bundle_data = await self.client._fetch_resource(self.resource_type, self.params)
        resources = self._get_bundle_resources(bundle_data)
        return resources

    async def fetch_raw(self):
        data = await self.client._fetch_resource(self.resource_type, self.params)
        data_resource_type = data.get("resourceType", None)

        if data_resource_type == "Bundle":
            for item in data["entry"]:
                item.resource = self._dict_to_resource(item.resource)

        return data

    async def fetch_all(self):
        return list(x async for x in self)

    async def get(self, id=None):
        searchset = self.limit(2)
        if id:
            warnings.warn(
                "parameter 'id' of method get() is deprecated "
                "and will be removed in future versions. "
                "Please use 'search(id='...').get()'",
                DeprecationWarning,
                stacklevel=2,
            )
            searchset = searchset.search(_id=id)
        res_data = await searchset.fetch()
        if len(res_data) == 0:
            raise ResourceNotFound("No resources found")
        if len(res_data) > 1:
            raise MultipleResourcesFound("More than one resource found")
        resource = res_data[0]
        return self._dict_to_resource(resource)

    async def count(self):
        new_params = copy.deepcopy(self.params)
        new_params["_count"] = 0
        new_params["_totalMethod"] = "count"

        return (await self.client._fetch_resource(self.resource_type, params=new_params))["total"]

    async def first(self):
        result = await self.limit(1).fetch()

        return result[0] if result else None

    async def get_or_create(self, resource):
        assert resource.resource_type == self.resource_type
        data, status_code = await self.client._do_request(
            "POST", self.resource_type, resource.serialize(), self.params, True
        )
        return data, (True if status_code == 201 else False)

    async def update(self, resource):
        # TODO: Support cases where resource with id is provided
        # accordingly to the https://build.fhir.org/http.html#cond-update
        assert resource.resource_type == self.resource_type
        data, status_code = await self.client._do_request(
            "PUT", self.resource_type, resource.serialize(), self.params, True
        )
        return data, (True if status_code == 201 else False)

    async def patch(self, resource):
        # TODO: Handle cases where resource with id is provided
        assert resource.resource_type == self.resource_type
        # TODO: Should we omit resourceType after serialization? (not to pollute history)
        return await self.client._do_request(
            "PATCH", self.resource_type, resource.serialize(), self.params
        )

    async def delete(self):
        return await self.client._do_request(
            "DELETE", self.resource_type, params=self.params, returning_status=True
        )

    async def __aiter__(self):
        next_link = None
        while True:
            if next_link:
                bundle_data = await self.client._fetch_resource(*parse_pagination_url(next_link))
            else:
                bundle_data = await self.client._fetch_resource(self.resource_type, self.params)
            new_resources = self._get_bundle_resources(bundle_data)
            next_link = get_by_path(bundle_data, ["link", {"relation": "next"}, "url"])

            for item in new_resources:
                yield item

            if not next_link:
                break


class SyncResource(BaseResource, ABC):
    def save(self, fields=None, search_params=None):
        data = self.serialize()
        if fields:  # Use FHIRPatch if fields for partial update are defined http://hl7.org/fhir/http.html#patch
            if not self.id:
                raise TypeError("Resource `id` is required for update operation")
            request_data = []
            for key in fields:
                operator = 'add'  # TODO add logic to support other operators
                request_data.append(
                    {
                        'op': operator,
                        'path': f'/{key}',
                        'value': data[key]
                    }
                )
            data = request_data
            method = "patch"
        else:
            method = "put" if self.id else "post"
        response_data = self.client._do_request(
            method, self._get_path(), data=data, params=search_params
        )
        if response_data:
            super(BaseResource, self).clear()
            super(BaseResource, self).update(
                **self.client.resource(self.resource_type, **response_data)
            )

    def create(self, **kwargs):
        self.save(search_params=kwargs)
        return self

    def update(self):
        if not self.id:
            raise TypeError("Resource `id` is required for update operation")
        self.save()

    def patch(self, **kwargs):
        super(BaseResource, self).update(**kwargs)
        self.save(fields=kwargs.keys())

    def delete(self):
        return self.client._do_request("delete", self._get_path())

    def refresh(self):
        data = self.client._do_request("get", self._get_path())
        super(BaseResource, self).clear()
        super(BaseResource, self).update(**data)

    def is_valid(self, raise_exception=False):
        data = self.client._do_request(
            "post", "{0}/$validate".format(self.resource_type), data=self.serialize()
        )
        if any(issue["severity"] in ["fatal", "error"] for issue in data["issue"]):
            if raise_exception:
                raise OperationOutcome(resource=data)
            return False
        return True

    def execute(self, operation, method="post", data=None, params=None):
        return self.client._do_request(
            method,
            "{0}/{1}".format(self._get_path(), operation),
            data=data,
            params=params,
        )


class AsyncResource(BaseResource, ABC):
    async def save(self, fields=None, search_params=None):
        data = self.serialize()
        if fields:
            if not self.id:
                raise TypeError("Resource `id` is required for update operation")
            data = {key: data[key] for key in fields}
            method = "patch"
        else:
            method = "put" if self.id else "post"

        response_data = await self.client._do_request(
            method, self._get_path(), data=data, params=search_params
        )
        if response_data:
            super(BaseResource, self).clear()
            super(BaseResource, self).update(
                **self.client.resource(self.resource_type, **response_data)
            )

    async def create(self, **kwargs):
        await self.save(search_params=kwargs)
        return self

    async def update(self):
        if not self.id:
            raise TypeError("Resource `id` is required for update operation")
        await self.save()

    async def patch(self, **kwargs):
        super(BaseResource, self).update(**kwargs)
        await self.save(fields=kwargs.keys())

    async def delete(self):
        return await self.client._do_request("delete", self._get_path())

    async def refresh(self):
        data = await self.client._do_request("get", self._get_path())
        super(BaseResource, self).clear()
        super(BaseResource, self).update(**data)

    async def to_resource(self):
        return super(AsyncResource, self).to_resource()

    async def is_valid(self, raise_exception=False):
        data = await self.client._do_request(
            "post", "{0}/$validate".format(self.resource_type), data=self.serialize()
        )
        if any(issue["severity"] in ["fatal", "error"] for issue in data["issue"]):
            if raise_exception:
                raise OperationOutcome(resource=data)
            return False
        return True

    async def execute(self, operation, method="post", **kwargs):
        return await self.client._do_request(
            method, "{0}/{1}".format(self._get_path(), operation), **kwargs
        )


class SyncReference(BaseReference, ABC):
    def to_resource(self):
        """
        Returns Resource instance for this reference
        from fhir server otherwise.
        """
        if not self.is_local:
            raise ResourceNotFound("Can not resolve not local resource")
        resource_data = self.client._do_request(
            "get", "{0}/{1}".format(self.resource_type, self.id)
        )
        return self._dict_to_resource(resource_data)

    def execute(self, operation, method="post", **kwargs):
        if not self.is_local:
            raise ResourceNotFound("Can not execute on not local resource")
        return self.client._do_request(
            method,
            "{0}/{1}/{2}".format(self.resource_type, self.id, operation),
            **kwargs,
        )


class AsyncReference(BaseReference, ABC):
    async def to_resource(self):
        """
        Returns Resource instance for this reference
        from fhir server otherwise.
        """
        if not self.is_local:
            raise ResourceNotFound("Can not resolve not local resource")
        resource_data = await self.client._do_request(
            "get", "{0}/{1}".format(self.resource_type, self.id)
        )
        return self._dict_to_resource(resource_data)

    async def execute(self, operation, method="post", **kwargs):
        if not self.is_local:
            raise ResourceNotFound("Can not execute on not local resource")
        return await self.client._do_request(
            method,
            "{0}/{1}/{2}".format(self.resource_type, self.id, operation),
            **kwargs,
        )
