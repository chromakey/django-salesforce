# django-salesforce
#
# by Phil Christensen
# (c) 2012 Working Today
# See LICENSE.md for details
#

"""
Salesforce object query customizations.
"""

import copy, urllib, logging, types, datetime, decimal

from django.core import serializers, exceptions
from django.conf import settings
from django.db.models import query
from django.db.models.sql import Query, constants
from django.utils.encoding import force_unicode
from django.db.backends.signals import connection_created
from django.core.serializers import python
from django.core.exceptions import ImproperlyConfigured

import restkit

from salesforce import auth
from salesforce.backend import compiler

try:
	import json
except ImportError, e:
	import simplejson as json

log = logging.getLogger(__name__)

API_STUB = '/services/data/v24.0'

def quoted_string_literal(s, d):
	"""
	According to the SQL standard, this should be all you need to do to escape any kind of string.
	"""
	try:
		return "'%s'" % (s.replace("'", "''"),)
	except TypeError, e:
		raise NotImplementedError("Cannot quote %r objects: %r" % (type(s), s))

def process_args(args):
	"""
	Perform necessary quoting on the arg list.
	"""
	def _escape(item, conv):
		return conv.get(type(item), conv[str])(item, conv)
	return tuple([_escape(x, conversions) for x in args])

class SalesforceQuerySet(query.QuerySet):
	"""
	Use a custom SQL compiler to generate SOQL-compliant queries.
	"""
	def iterator(self):
		"""
		An iterator over the results from applying this QuerySet to the
		remote web service.
		"""
		from django.db import connections
		sql, params = compiler.SQLCompiler(self.query, connections[self.db], None).as_sql()
		cursor = CursorWrapper(connections[self.db], self.query)
		cursor.execute(sql, params)
		
		def _mkmodels(data):
			for record in data:
				attribs = record.pop('attributes')
				
				mod = self.model.__module__.split('.')
				if(mod[-1] == 'models'):
					app_name = mod[-2]
				elif(hasattr(self.model._meta, 'app_name')):
					app_name = getattr(self.model._meta, 'app_name')
				else:
					raise ImproperlyConfigured("Can't discover the app_name for %s, you must specify it via model meta options.")
				
				yield dict(
					model	= '.'.join([app_name, self.model.__name__]),
					pk		= record.pop('Id'),
					fields	= dict([(x.name, record[x.column]) for x in self.model._meta.fields if not x.primary_key]),
				)
		
		response = cursor.fetchmany(constants.GET_ITERATOR_CHUNK_SIZE)
		for res in python.Deserializer(_mkmodels(response)):
			yield res.object

class SalesforceQuery(Query):
	"""
	Override aggregates.
	"""
	from salesforce.backend import aggregates
	aggregates_module = aggregates
	
	def has_results(self, using):
		q = self.clone()
		compiler = q.get_compiler(using=using)
		return bool(compiler.execute_sql(constants.SINGLE))

class CursorWrapper(object):
	"""
	A wrapper that emulates the behavior of a database cursor.
	
	This is the class that is actually responsible for making connections
	to the SF REST API
	"""
	def __init__(self, conn, query):
		"""
		Connect to the Salesforce API.
		"""
		connection_created.send(sender=self.__class__, connection=self)
		self.oauth = auth.authenticate(conn.settings_dict)
		self.query = query
		self.results = iter([])
		self.rowcount = None
	
	def execute(self, q, args=None):
		"""
		Send a query to the Salesforce API.
		"""
		from salesforce.backend import base
		
		headers = dict()
		headers['Authorization'] = 'OAuth %s' % self.oauth['access_token']
		
		processed_sql = q % process_args(args)
		log.debug(processed_sql)
		
		url = None
		post_data = dict()
		if(q.upper().startswith('SELECT')):
			method = 'query'
			url = u'%s%s?%s' % (self.oauth['instance_url'], '%s/query' % API_STUB, urllib.urlencode(dict(
				q	= processed_sql,
			)))
		elif(q.upper().startswith('INSERT')):
			method = 'insert'
			table = compiler.process_name(self.query.model._meta.db_table)
			url = self.oauth['instance_url'] + API_STUB + ('/sobjects/%s/' % table)
			post_data = dict([x for x in zip(self.query.columns, self.query.params) if x[0] != 'Id'])
			headers['Content-Type'] = 'application/json'
		elif(q.upper().startswith('UPDATE')):
			method = 'update'
			pk = self.query.where.children[0].children[0][-1]
			table = compiler.process_name(self.query.model._meta.db_table)
			url = self.oauth['instance_url'] + API_STUB + ('/sobjects/%s/%s' % (table, pk))
			post_data = dict([(x[0].name, x[2]) for x in self.query.values if x[0].name != 'Id'])
			headers['Content-Type'] = 'application/json'
		elif(q.upper().startswith('DELETE')):
			method = 'delete'
			pk = self.query.where.children[0][-1][0]
			table = compiler.process_name(self.query.model._meta.db_table)
			url = self.oauth['instance_url'] + API_STUB + ('/sobjects/%s/%s' % (table, pk))
		else:
			raise base.DatabaseError("Unsupported query: %s" % debug_sql)
		
		resource = restkit.Resource(url)
		log.debug('Request API URL: %s' % url)
		
		try:
			if(method == 'query'):
				response = resource.get(headers=headers)
			elif(method == 'insert'):
				response = resource.post(headers=headers, payload=json.dumps(post_data))
			elif(method == 'delete'):
				response = resource.delete(headers=headers)
			else:#(method == 'update')
				response = resource.request(method='patch', headers=headers, payload=json.dumps(post_data))
		except restkit.ResourceNotFound, e:
			log.error("Couldn't connect to Salesforce API (404): %s" % e)
			return
		except restkit.ResourceGone, e:
			log.error("Couldn't connect to Salesforce API (410): %s" % e)
			return
		except restkit.Unauthorized, e:
			raise exceptions.PermissionDenied(str(e))
		except restkit.RequestFailed, e:
			data = json.loads(str(e))[0]
			if(data['errorCode'] == 'INVALID_FIELD'):
				raise exceptions.FieldError(data['message'])
			elif(data['errorCode'] == 'MALFORMED_QUERY'):
				raise SyntaxError(data['message'])
			elif(data['errorCode'] == 'INVALID_FIELD_FOR_INSERT_UPDATE'):
				raise base.IntegrityError(data['message'])
			elif(data['errorCode'] == 'METHOD_NOT_ALLOWED'):
				raise base.DatabaseError("[%s] %s" % (url, data['message']))
			else:
				raise base.DatabaseError(str(data))
		
		body = response.body_string()
		jsrc = force_unicode(body).encode(settings.DEFAULT_CHARSET)
		
		try:
			data = json.loads(jsrc)
		except Exception, e:
			if(method not in ('delete', 'update')):
				raise e
			else:
				data = []
		
		def _iterate(d):
			for record in d['records']:
				yield record
		
		if('totalSize' in data):
			self.rowcount = data['totalSize']
		elif('errorCode' in data):
			raise base.DatabaseError(data['message'])
		elif(method == 'insert'):
			if(data['success']):
				self.lastrowid = data['id']
			else:
				raise base.DatabaseError(data['errors'])
		
		if('count()' in q.lower()):
			# COUNT() queries in SOQL are a special case, as they don't actually return rows
			data['records'] = [{self.rowcount:'COUNT'}]
		
		self.results = _iterate(data)
	
	def fetchone(self):
		"""
		Fetch a single result from a previously executed query.
		"""
		try:
			res = self.results.next()
			return res
		except StopIteration:
			return None
	
	def fetchmany(self, size=0):
		"""
		Fetch multiple results from a previously executed query.
		"""
		result = []
		counter = 0
		while(True):
			try:
				if(counter == size-1):
					return result
				if(size != 0):
					counter += 1
				row = self.fetchone()
				if not(row):
					return result
				result.append(row)
			except StopIteration:
				pass
		return result

	def fetchall(self):
		"""
		Fetch all results from a previously executed query.
		"""
		result = []
		for index in range(size):
			try:
				result.append(self.fetchone())
			except StopIteration:
				pass
		return result

string_literal = quoted_string_literal

# supported types
conversions = {
	int: lambda s,d: str(s),
	long: lambda s,d: str(s),
	float: lambda o,d: '%.15g' % o,
	types.NoneType: lambda s,d: 'NULL',
	list: lambda s,d: '(%s)' % ','.join([escape_item(x, conversions) for x in s]),
	tuple: lambda s,d: '(%s)' % ','.join([escape_item(x, conversions) for x in s]),
	str: lambda o,d: string_literal(o, d), # default
	unicode: lambda s,d: string_literal(s.encode(), d),
	bool: lambda s,d: str(int(s)),
	datetime.date: lambda d,c: string_literal(date.strftime(d, "%Y-%m-%d"), c),
	datetime.datetime: lambda d,c: string_literal(date.strftime(d, "%Y-%m-%dT%H:%M:%S.000-0000"), c),
	datetime.timedelta: lambda v,c: string_literal('%d %d:%d:%d' % (v.days, int(v.seconds / 3600) % 24, int(v.seconds / 60) % 60, int(v.seconds) % 60)),
	decimal.Decimal: lambda s,d: str(s),
}
