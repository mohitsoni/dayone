from celery import task, current_task
from celery.result import AsyncResult

from django.http import HttpResponse, HttpResponseRedirect

import lib, settings, util
from models import *

import urllib, json, os, re, time, dateutil, datetime, plistlib

#should spawn more tasks to fetch individual posts
@task
def sync_meta( user_profile, *kargs ):
	time.sleep(1)

	dapi = lib.DropboxAPI( user=user_profile.user )
	# dapi.metadata( user_profile.entries_path )
	metadat_request = dapi.request('https://api.dropbox.com/1/metadata/dropbox/' + user_profile.entries_path ).to_url()
	metadata_response = urllib.urlopen(metadat_request).read()

	user_profile.entries_meta = metadata_response
	user_profile.entries_last_sync = time.time()
	user_profile.save()

	# trigger post_refresh_in_progress=True

	print "lets see", user_profile.entries_path
	print user_profile.user.first_name

	sync_data.delay( user_profile )

	return str(kargs)
	pass

@task
def sync_data( user_profile, *kargs, **kwargs ):
	print 'sync data start'
	# this will call sync_post for each post
	user_status = Status.factory(user_profile.user)

	user_status.set('post_refresh_in_progress','False')
	post_refresh_status = user_status.get('post_refresh_in_progress')

	pub_tag = user_profile.pub_tag
	anon_tag = user_profile.anon_tag
	# if refresh is not in progress, initiate it
	print 'about_to_refresh'
	if post_refresh_status.value != 'True':
		print 'is_refreshing'
		user_status.set('post_refresh_in_progress','True')

		# figure out when the posts in the db were last synced!
		# TODO: Also pick posts whose public/anonymous status may have changed due to change in tag_names!
		db_uuid_last_sync_map = {}
		db_uuid_post_map = {}
		user_posts = Post.objects.filter(user_id=user_profile.user_id)
		for each_post in user_posts:
			db_uuid_last_sync_map[ each_post.uuid ] = each_post.last_sync
			db_uuid_post_map[ each_post.uuid ] = each_post

		# figure out the last modified times of all posts from metadata
		meta_last_modified_map = {}
		entries_meta = json.loads(user_profile.entries_meta)
		for each in entries_meta['contents']:
			last_modified_datetime = dateutil.parser.parse( each['modified'] )
			last_modified = (last_modified_datetime.replace(tzinfo=None) - datetime.datetime(1970,1,1)).total_seconds()
			uuid_file = os.path.basename(each['path'])
			meta_last_modified_map[ uuid_file ] = last_modified

		# fetch permanent urls first
		entries_html = urllib.urlopen(user_profile.entries_share_url).read()
		# entries_html = open('entries_html.html','r').read()
		share_uuid_links = set( re.findall( 'https:\/\/www.dropbox.*?.doentry', entries_html ) )
		share_uuid_names = []
		uuid_share_path_map = {}
		for each_share_link in share_uuid_links:
			share_uuid_name = os.path.basename(each_share_link)
			share_uuid_names.append( share_uuid_name )
			uuid_share_path_map[ share_uuid_name ] = each_share_link.replace('www.dropbox','dl.dropboxusercontent')


		# All the 3 maps have uuids as keys
		# print len(meta_last_modified_map.keys())
		# print len(uuid_share_path_map.keys())
		# print len(db_uuid_last_sync_map.keys())
		# print json.dumps(meta_last_modified_map,indent=4)
		# print json.dumps(uuid_share_path_map,indent=4)
		# print json.dumps(db_uuid_last_sync_map,indent=4)

		# posts that need to be tasked out and fetched
		# 	- posts modified after last sync
		#   - posts whose tag status has been changed
		# 	- posts not in db
		uuids_to_task = []
		all_uuids = meta_last_modified_map.keys()
		for each_uuid in all_uuids:
			# we don't have it in db
			# then just task it out
			if not db_uuid_post_map.has_key(each_uuid):
				uuids_to_task.append( each_uuid )
			# we have it in db
			# then see if its not already in queue
			elif db_uuid_last_sync_map.has_key(each_uuid):
				# if its not in queue
				# check if its sync date is before meta modificatio ndate
				if db_uuid_last_sync_map[each_uuid] < meta_last_modified_map[each_uuid]:
					uuids_to_task.append( each_uuid )
				# check if it still satisfies the public/anonymous structure
				else:
					post = db_uuid_post_map[each_uuid]
					# double check this!!
					if ((post.is_public and pub_tag not in post.all_tags) or (not post.is_public and pub_tag in post.all_tags) 
						or (post.is_anonymous and anon_tag not in post.all_tags) or (not post.is_anonymous and anon_tag in post.all_tags)):
							uuids_to_task.append( each_uuid )


		print 'UUIDS_TO_TASK'
		print json.dumps(uuids_to_task,indent=4)

		# then create tasks to fetch
		for each_uuid in uuids_to_task:
			# 2 choices
			# 	- create post here and set share_url here
			#		* makes sense because some may already have post objects
			#	- create post in the scheduled task itself

			post_object = None
			if db_uuid_post_map.has_key(each_uuid):
				print 'post exists'
				post_object = db_uuid_post_map[ each_uuid ]
			else:
				post_object = Post.objects.create(
					user_id = user_profile.user_id,
					uuid = each_uuid,
					entry_share_url = uuid_share_path_map[ each_uuid ],
				)

			if post_object:
				post_object.sync_ready = True
				post_object.sync_complete = False
				post_object.save()
				print 'TO_SYNC', post_object.uuid
				sync_post.delay( user_profile, post_object )
			else:
				# report error
				pass

		user_status.set('post_refresh_in_progress','False')
	print 'sync data end'
	pass

@task
def sync_post( user_profile, post_object ):
	print "SYNC_POST", post_object.uuid
	content = urllib.urlopen( post_object.entry_share_url ).read()
	try:
		content_json = plistlib.readPlistFromString(content)
	except:
		content = content.replace('UTF-8','UTF-16')
		content_json = plistlib.readPlistFromString(content)

	util.clean_keys(content_json)

	# post_object.content = content
	# post_object.post_json = json.dumps( json )
	post_object.sync_ready = False
	post_object.sync_complete = True
	post_object.last_sync = time.time()

	post_tags = content_json.get('tags',[])
	post_tags = [ each.lower() for each in post_tags ]
	post_object.all_tags = ",".join(post_tags)


	if user_profile.anon_tag.lower().strip() in post_tags:
		post_object.is_anonymous = True

	if user_profile.pub_tag.lower().strip() in post_tags:
		post_object.is_public = True

	if post_object.is_anonymous or post_object.is_public:
		post_object.content = util.json_dumps(content_json)
	else:
		post_object.content = ''

	# TODO: Think about support for multiple tags
	# post_object.is_public = False
	# user_pub_tags = user_profile.pub_tag.lower().split(',')
	# user_pub_tags = map( lambda item: item.strip(), user_pub_tags )
	# for each_pub_tag in user_pub_tags:
	# 	if each_pub_tag in post_tags:
	# 		post_object.is_public = True

	post_object.save()
	pass